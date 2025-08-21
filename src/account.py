import os
import inspect
from dotenv import load_dotenv
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient
from x10.perpetual.configuration import STARKNET_MAINNET_CONFIG

load_dotenv()


def _require_env_vars() -> tuple[str, str, str, str]:
    """Fetch and validate required environment variables."""

    api_key = os.getenv("API_KEY")
    public_key = os.getenv("PUBLIC_KEY")
    private_key = os.getenv("PRIVATE_KEY")
    vault = os.getenv("VAULT")

    if not api_key:
        raise RuntimeError("Environment variable 'API_KEY' is missing or empty")
    if not public_key:
        raise RuntimeError("Environment variable 'PUBLIC_KEY' is missing or empty")
    if not private_key:
        raise RuntimeError("Environment variable 'PRIVATE_KEY' is missing or empty")
    if not vault:
        raise RuntimeError("Environment variable 'VAULT' is missing or empty")

    return api_key, public_key, private_key, vault

class TradingAccount:
    def __init__(self):
        api_key, public_key, private_key, vault = _require_env_vars()

        self.endpoint_config = STARKNET_MAINNET_CONFIG

        self.api_key = api_key
        self.public_key = public_key
        self.private_key = private_key
        self.vault = int(vault)

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

    async def close(self) -> None:
        """Close underlying HTTP sessions for created clients."""
        for client in (self.async_client, self.blocking_client):
            close_method = getattr(client, "close", None)
            if close_method:
                if inspect.iscoroutinefunction(close_method):
                    await close_method()
                else:
                    close_method()
