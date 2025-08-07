import os
from dotenv import load_dotenv, dotenv_values 
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.simple_client.simple_trading_client import BlockingTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG

load_dotenv()

api_key:str = os.getenv("API_KEY")
public_key:str = os.getenv("PUBLIC_KEY")
private_key:str = os.getenv("PRIVATE_KEY")
vault:int = int(os.getenv("VAULT"))

stark_account = StarkPerpetualAccount(
    vault=vault,
    private_key=private_key,
    public_key=public_key,
    api_key=api_key,
)

trading_client = PerpetualTradingClient(
    endpoint_config=MAINNET_CONFIG,
    stark_account=stark_account,
)

blocking_client = BlockingTradingClient(
        endpoint_config=MAINNET_CONFIG,
        account=stark_account,
    )