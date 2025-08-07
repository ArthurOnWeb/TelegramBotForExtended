import os
from dotenv import load_dotenv
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG

load_dotenv()

class TradingAccount:
    def __init__(self):
        self.endpoint_config = MAINNET_CONFIG

        self.api_key = os.getenv("API_KEY")
        self.public_key = os.getenv("PUBLIC_KEY")
        self.private_key = os.getenv("PRIVATE_KEY")
        self.vault = int(os.getenv("VAULT"))

        self.stark_account = StarkPerpetualAccount(
            vault=self.vault,
            private_key=self.private_key,
            public_key=self.public_key,
            api_key=self.api_key,
        )

        self.async_client = PerpetualTradingClient(
            endpoint_config=self.endpoint_config,
            stark_account=self.stark_account,
        )

        self.blocking_client = BlockingTradingClient(
            endpoint_config=self.endpoint_config,
            account=self.stark_account,
        )

    def get_async_client(self) -> PerpetualTradingClient:
        return self.async_client

    def get_blocking_client(self) -> BlockingTradingClient:
        return self.blocking_client

    def get_account(self) -> StarkPerpetualAccount:
        return self.stark_account
