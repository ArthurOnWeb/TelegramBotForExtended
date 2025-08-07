import os
import inspect
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

    async def close(self) -> None:
        """Close underlying HTTP sessions for created clients.

        Some client implementations expose a ``close_session`` coroutine instead
        of a plain ``close`` method.  To be defensive we try a few common method
        names and fall back to closing an explicit ``session`` attribute when
        present.  This ensures we properly release network resources and avoid
        warnings about unclosed sessions.
        """

        async def _maybe_close(obj, *method_names):
            for name in method_names:
                method = getattr(obj, name, None)
                if method:
                    if inspect.iscoroutinefunction(method):
                        await method()
                    else:
                        method()
                    return True
            return False

        for client in (self.async_client, self.blocking_client):
            closed = await _maybe_close(client, "close", "close_session", "aclose")
            if not closed:
                session = getattr(client, "session", None)
                if session:
                    await _maybe_close(session, "close", "aclose")