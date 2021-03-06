import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast

import gevent

from rotkehlchen.accounting.events import TaxableEvents
from rotkehlchen.accounting.structures import DefiEvent
from rotkehlchen.assets.asset import Asset
from rotkehlchen.assets.unknown_asset import UnknownEthereumToken
from rotkehlchen.chain.ethereum.trades import AMMTrade
from rotkehlchen.constants.assets import A_BTC, A_ETH
from rotkehlchen.csv_exporter import CSVExporter
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.db.settings import DBSettings
from rotkehlchen.errors import (
    DeserializationError,
    NoPriceForGivenTimestamp,
    PriceQueryUnsupportedAsset,
    RemoteError,
    UnknownAsset,
    UnsupportedAsset,
)
from rotkehlchen.exchanges.data_structures import (
    AssetMovement,
    Loan,
    MarginPosition,
    Trade,
    TradeType,
)
from rotkehlchen.fval import FVal
from rotkehlchen.history import PriceHistorian
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.typing import EthereumTransaction, Fee, Timestamp
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.accounting import (
    TaxableAction,
    action_get_assets,
    action_get_timestamp,
    action_get_type,
)
from rotkehlchen.utils.misc import timestamp_to_date

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


class Accountant():

    def __init__(
            self,
            db: DBHandler,
            user_directory: Path,
            msg_aggregator: MessagesAggregator,
            create_csv: bool,
    ) -> None:
        log.debug('Initializing Accountant')
        self.db = db
        profit_currency = db.get_main_currency()
        self.msg_aggregator = msg_aggregator
        self.csvexporter = CSVExporter(profit_currency, user_directory, create_csv)
        self.events = TaxableEvents(self.csvexporter, profit_currency)

        self.asset_movement_fees = FVal(0)
        self.last_gas_price = 0

        self.started_processing_timestamp = Timestamp(-1)
        self.currently_processing_timestamp = Timestamp(-1)

    def __del__(self) -> None:
        del self.events
        del self.csvexporter

    @property
    def general_trade_pl(self) -> FVal:
        return self.events.general_trade_profit_loss

    @property
    def taxable_trade_pl(self) -> FVal:
        return self.events.taxable_trade_profit_loss

    def _customize(self, settings: DBSettings) -> None:
        """Customize parameters after pulling DBSettings"""
        if settings.include_crypto2crypto is not None:
            self.events.include_crypto2crypto = settings.include_crypto2crypto

        if settings.taxfree_after_period is not None:
            given_taxfree_after_period: Optional[int] = settings.taxfree_after_period
            if given_taxfree_after_period == -1:
                # That means user requested to disable taxfree_after_period
                given_taxfree_after_period = None

            self.events.taxfree_after_period = given_taxfree_after_period

        self.profit_currency = settings.main_currency
        self.events.profit_currency = settings.main_currency
        self.csvexporter.profit_currency = settings.main_currency

        if settings.account_for_assets_movements is not None:
            self.events.account_for_assets_movements = settings.account_for_assets_movements

    def get_fee_in_profit_currency(self, trade: Trade) -> Fee:
        """Get the profit_currency rate of the fee of the given trade

        May raise:
        - PriceQueryUnsupportedAsset if from/to asset is missing from all price oracles
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from the price oracle
        - RemoteError if there is a problem reaching the price oracle server
        or with reading the response returned by the server
        """
        fee_rate = PriceHistorian().query_historical_price(
            from_asset=trade.fee_currency,
            to_asset=self.profit_currency,
            timestamp=trade.timestamp,
        )
        return Fee(fee_rate * trade.fee)

    def add_asset_movement_to_events(self, movement: AssetMovement) -> None:
        """
        Adds the given asset movement to the processed events

        May raise:
        - PriceQueryUnsupportedAsset if from/to asset is missing from all price oracles
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from cryptocompare
        - RemoteError if there is a problem reaching the price oracle server
        or with reading the response returned by the server
        """
        timestamp = movement.timestamp
        if timestamp < self.start_ts:
            return

        if movement.asset.identifier == 'KFEE' or not self.events.account_for_assets_movements:
            # There is no reason to process deposits of KFEE for kraken as it has only value
            # internal to kraken and KFEE has no value and will error at cryptocompare price query
            return

        fee_rate = self.events.get_rate_in_profit_currency(movement.fee_asset, timestamp)
        cost = movement.fee * fee_rate
        self.asset_movement_fees += cost
        log.debug(
            'Accounting for asset movement',
            sensitive_log=True,
            category=movement.category,
            asset=movement.asset,
            cost_in_profit_currency=cost,
            timestamp=timestamp,
            exchange_name=movement.location,
        )

        self.csvexporter.add_asset_movement(
            exchange=movement.location,
            category=movement.category,
            asset=movement.asset,
            fee=movement.fee,
            rate=fee_rate,
            timestamp=timestamp,
        )

    def account_for_gas_costs(
            self,
            transaction: EthereumTransaction,
            include_gas_costs: bool,
    ) -> None:
        """
        Accounts for the gas costs of the given ethereum transaction

        May raise:
        - PriceQueryUnsupportedAsset if from/to assets are missing from all price oracles
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from cryptocompare
        - RemoteError if there is a problem reaching the price oracle server
        or with reading the response returned by the server
        """
        if not include_gas_costs:
            return
        if transaction.timestamp < self.start_ts:
            return

        if transaction.gas_price == -1:
            gas_price = self.last_gas_price
        else:
            gas_price = transaction.gas_price
            self.last_gas_price = transaction.gas_price

        rate = self.events.get_rate_in_profit_currency(A_ETH, transaction.timestamp)
        eth_burned_as_gas = FVal(transaction.gas_used * gas_price) / FVal(10 ** 18)
        cost = eth_burned_as_gas * rate
        self.eth_transactions_gas_costs += cost

        log.debug(
            'Accounting for ethereum transaction gas cost',
            sensitive_log=True,
            gas_used=transaction.gas_used,
            gas_price=gas_price,
            timestamp=transaction.timestamp,
        )

        self.csvexporter.add_tx_gas_cost(
            transaction_hash=transaction.tx_hash,
            eth_burned_as_gas=eth_burned_as_gas,
            rate=rate,
            timestamp=transaction.timestamp,
        )

    def trade_add_to_sell_events(self, trade: Trade, loan_settlement: bool) -> None:
        """
        Adds the given trade to the sell events

        May raise:
        - PriceQueryUnsupportedAsset if from/to asset is missing from all price oracles
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from cryptocompare
        - RemoteError if there is a problem reaching the price oracle server
        or with reading the response returned by the server
        """
        selling_asset = trade.base_asset
        receiving_asset = trade.quote_asset
        receiving_asset_rate = self.events.get_rate_in_profit_currency(
            receiving_asset,
            trade.timestamp,
        )
        selling_rate = receiving_asset_rate * trade.rate
        fee_in_profit_currency = self.get_fee_in_profit_currency(trade)
        gain_in_profit_currency = selling_rate * trade.amount

        if not loan_settlement:
            self.events.add_sell_and_corresponding_buy(
                location=trade.location,
                selling_asset=selling_asset,
                selling_amount=trade.amount,
                receiving_asset=receiving_asset,
                receiving_amount=trade.amount * trade.rate,
                gain_in_profit_currency=gain_in_profit_currency,
                total_fee_in_profit_currency=fee_in_profit_currency,
                trade_rate=trade.rate,
                rate_in_profit_currency=selling_rate,
                timestamp=trade.timestamp,
            )
        else:
            self.events.add_sell(
                location=trade.location,
                selling_asset=selling_asset,
                selling_amount=trade.amount,
                receiving_asset=None,
                receiving_amount=None,
                gain_in_profit_currency=gain_in_profit_currency,
                total_fee_in_profit_currency=fee_in_profit_currency,
                trade_rate=trade.rate,
                rate_in_profit_currency=selling_rate,
                timestamp=trade.timestamp,
                loan_settlement=True,
                is_virtual=False,
            )

    def process_history(
            self,
            start_ts: Timestamp,
            end_ts: Timestamp,
            trade_history: List[Union[Trade, MarginPosition, AMMTrade]],
            loan_history: List[Loan],
            asset_movements: List[AssetMovement],
            eth_transactions: List[EthereumTransaction],
            defi_events: List[DefiEvent],
    ) -> Dict[str, Any]:
        """Processes the entire history of cryptoworld actions in order to determine
        the price and time at which every asset was obtained and also
        the general and taxable profit/loss.

        start_ts here is the timestamp at which to start taking trades and other
        taxable events into account. Not where processing starts from. Processing
        always starts from the very first event we find in the history.
        """
        log.info(
            'Start of history processing',
            start_ts=start_ts,
            end_ts=end_ts,
        )
        self.events.reset(start_ts, end_ts)
        self.last_gas_price = 2000000000
        self.start_ts = start_ts
        self.eth_transactions_gas_costs = FVal(0)
        self.asset_movement_fees = FVal(0)
        self.csvexporter.reset_csv_lists()

        # Ask the DB for the settings once at the start of processing so we got the
        # same settings through the entire task
        db_settings = self.db.get_settings()
        self._customize(db_settings)

        actions: List[TaxableAction] = list(trade_history)
        # If we got loans, we need to interleave them with the full history and re-sort
        if len(loan_history) != 0:
            actions.extend(loan_history)

        if len(asset_movements) != 0:
            actions.extend(asset_movements)

        if len(eth_transactions) != 0:
            actions.extend(eth_transactions)

        if len(defi_events) != 0:
            actions.extend(defi_events)

        actions.sort(
            key=lambda action: action_get_timestamp(action),
        )
        # The first ts is the ts of the first action we have in history or 0 for empty history
        first_ts = Timestamp(0) if len(actions) == 0 else action_get_timestamp(actions[0])
        self.currently_processing_timestamp = first_ts
        self.started_processing_timestamp = first_ts

        prev_time = Timestamp(0)
        count = 0
        for action in actions:
            try:
                (
                    should_continue,
                    prev_time,
                ) = self.process_action(action, end_ts, prev_time, db_settings)
            except PriceQueryUnsupportedAsset as e:
                ts = action_get_timestamp(action)
                self.msg_aggregator.add_error(
                    f'Skipping action at '
                    f' {timestamp_to_date(ts, formatstr="%d/%m/%Y, %H:%M:%S")} '
                    f'during history processing due to an asset unknown to '
                    f'cryptocompare being involved. Check logs for details',
                )
                log.error(
                    f'Skipping action {str(action)} during history processing due to '
                    f'cryptocompare not supporting an involved asset: {str(e)}',
                )
                continue
            except NoPriceForGivenTimestamp as e:
                ts = action_get_timestamp(action)
                self.msg_aggregator.add_error(
                    f'Skipping action at '
                    f' {timestamp_to_date(ts, formatstr="%d/%m/%Y, %H:%M:%S")} '
                    f'during history processing due to inability to find a price '
                    f'at that point in time: {str(e)}. Check the logs for more details',
                )
                log.error(
                    f'Skipping action {str(action)} during history processing due to '
                    f'inability to query a price at that time: {str(e)}',
                )
                continue
            except RemoteError as e:
                ts = action_get_timestamp(action)
                self.msg_aggregator.add_error(
                    f'Skipping action at '
                    f' {timestamp_to_date(ts, formatstr="%d/%m/%Y, %H:%M:%S")} '
                    f'during history processing due to inability to reach an external '
                    f'service at that point in time: {str(e)}. Check the logs for more details',
                )
                log.error(
                    f'Skipping action {str(action)} during history processing due to '
                    f'inability to reach an external service at that time: {str(e)}',
                )
                continue

            if not should_continue:
                break

            if count % 500 == 0:
                # This loop can take a very long time depending on the amount of actions
                # to process. We need to yield to other greenlets or else calls to the
                # API may time out
                gevent.sleep(0.5)
            count += 1

        self.events.calculate_asset_details()
        Inquirer().save_historical_forex_data()

        sum_other_actions = (
            self.events.margin_positions_profit_loss +
            self.events.defi_profit_loss +
            self.events.loan_profit -
            self.events.settlement_losses -
            self.asset_movement_fees -
            self.eth_transactions_gas_costs
        )
        total_taxable_pl = self.events.taxable_trade_profit_loss + sum_other_actions
        return {
            'overview': {
                'defi_profit_loss': str(self.events.defi_profit_loss),
                'loan_profit': str(self.events.loan_profit),
                'margin_positions_profit_loss': str(self.events.margin_positions_profit_loss),
                'settlement_losses': str(self.events.settlement_losses),
                'ethereum_transaction_gas_costs': str(self.eth_transactions_gas_costs),
                'asset_movement_fees': str(self.asset_movement_fees),
                'general_trade_profit_loss': str(self.events.general_trade_profit_loss),
                'taxable_trade_profit_loss': str(self.events.taxable_trade_profit_loss),
                'total_taxable_profit_loss': str(total_taxable_pl),
                'total_profit_loss': str(
                    self.events.general_trade_profit_loss +
                    sum_other_actions,
                ),
            },
            'all_events': self.csvexporter.all_events,
        }

    def process_action(
            self,
            action: TaxableAction,
            end_ts: Timestamp,
            prev_time: Timestamp,
            db_settings: DBSettings,
    ) -> Tuple[bool, Timestamp]:
        """Processes each individual action and returns whether we should continue
        looping through the rest of the actions or not

        May raise:
        - PriceQueryUnsupportedAsset if from/to asset is missing from price oracles
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from cryptocompare
        - RemoteError if there is a problem reaching the price oracle server
        or with reading the response returned by the server
        """
        ignored_assets = self.db.get_ignored_assets()

        # Assert we are sorted in ascending time order.
        timestamp = action_get_timestamp(action)
        assert timestamp >= prev_time, (
            "During history processing the trades/loans are not in ascending order"
        )
        prev_time = timestamp

        if timestamp > end_ts:
            return False, prev_time

        self.currently_processing_timestamp = timestamp

        action_type = action_get_type(action)

        try:
            asset1, asset2 = action_get_assets(action)
        except UnknownAsset as e:
            self.msg_aggregator.add_warning(
                f'At history processing found trade with unknown asset {e.asset_name}. '
                f'Ignoring the trade.',
            )
            return True, prev_time
        except UnsupportedAsset as e:
            self.msg_aggregator.add_warning(
                f'At history processing found trade with unsupported asset {e.asset_name}. '
                f'Ignoring the trade.',
            )
            return True, prev_time
        except DeserializationError:
            self.msg_aggregator.add_error(
                'At history processing found trade with non string asset type. '
                'Ignoring the trade.',
            )
            return True, prev_time

        if isinstance(asset1, UnknownEthereumToken) or isinstance(asset2, UnknownEthereumToken):  # type: ignore  # noqa: E501
            # TODO: Typing needs fixing here  # type: ignore
            log.debug(  # type: ignore
                'Ignoring action with unknown token',
                action_type=action_type,
                asset1=asset1,
                asset2=asset2,
            )
            return True, prev_time

        if asset1 in ignored_assets or asset2 in ignored_assets:
            log.debug(
                'Ignoring action with ignored asset',
                action_type=action_type,
                asset1=asset1,
                asset2=asset2,
            )
            return True, prev_time

        if action_type == 'loan':
            action = cast(Loan, action)
            self.events.add_loan_gain(
                location=action.location,
                gained_asset=action.currency,
                lent_amount=action.amount_lent,
                gained_amount=action.earned,
                fee_in_asset=action.fee,
                open_time=action.open_time,
                close_time=timestamp,
            )
            return True, prev_time
        if action_type == 'asset_movement':
            action = cast(AssetMovement, action)
            self.add_asset_movement_to_events(action)
            return True, prev_time
        if action_type == 'margin_position':
            action = cast(MarginPosition, action)
            self.events.add_margin_position(margin=action)
            return True, prev_time
        if action_type == 'ethereum_transaction':
            action = cast(EthereumTransaction, action)
            self.account_for_gas_costs(action, db_settings.include_gas_costs)
            return True, prev_time
        if action_type == 'defi_event':
            action = cast(DefiEvent, action)
            self.events.add_defi_event(action)
            return True, prev_time

        # else if we get here it's a trade
        trade = cast(Trade, action)
        # When you buy, you buy with the cost_currency and receive the other one
        # When you sell, you sell the amount in non-cost_currency and receive
        # costs in cost_currency
        if trade.trade_type == TradeType.BUY:
            self.events.add_buy_and_corresponding_sell(
                location=trade.location,
                bought_asset=trade.base_asset,
                bought_amount=trade.amount,
                paid_with_asset=trade.quote_asset,
                trade_rate=trade.rate,
                fee_in_profit_currency=self.get_fee_in_profit_currency(trade),
                fee_currency=trade.fee_currency,
                timestamp=trade.timestamp,
            )
        elif trade.trade_type == TradeType.SELL:
            self.trade_add_to_sell_events(trade, False)
        elif trade.trade_type == TradeType.SETTLEMENT_SELL:
            # in poloniex settlements sell some asset to get BTC to repay a loan
            self.trade_add_to_sell_events(trade, True)
        elif trade.trade_type == TradeType.SETTLEMENT_BUY:
            # in poloniex settlements you buy some asset with BTC to repay a loan
            # so in essense you sell BTC to repay the loan
            selling_asset = A_BTC
            selling_asset_rate = self.events.get_rate_in_profit_currency(
                selling_asset,
                trade.timestamp,
            )
            selling_rate = selling_asset_rate * trade.rate
            fee_in_profit_currency = self.get_fee_in_profit_currency(trade)
            gain_in_profit_currency = selling_rate * trade.amount
            # Since the original trade is a buy of some asset with BTC, then the
            # when we invert the sell, the sold amount of BTC should be the cost
            # (amount*rate) of the original buy
            selling_amount = trade.rate * trade.amount
            self.events.add_sell(
                location=trade.location,
                selling_asset=selling_asset,
                selling_amount=selling_amount,
                receiving_asset=None,
                receiving_amount=None,
                gain_in_profit_currency=gain_in_profit_currency,
                total_fee_in_profit_currency=fee_in_profit_currency,
                trade_rate=trade.rate,
                rate_in_profit_currency=selling_asset_rate,
                timestamp=trade.timestamp,
                loan_settlement=True,
            )
        else:
            # Should never happen
            raise AssertionError(f'Unknown trade type "{trade.trade_type}" encountered')

        return True, prev_time

    def get_calculated_asset_amount(self, asset: Asset) -> Optional[FVal]:
        """Get the amount of asset accounting has calculated we should have after
        the history has been processed
        """
        if asset not in self.events.events:
            return None

        amount = FVal(0)
        for buy_event in self.events.events[asset].buys:
            amount += buy_event.amount
        return amount
