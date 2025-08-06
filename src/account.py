import os
from dotenv import load_dotenv, dotenv_values 
from x10.perpetual.accounts import StarkPerpetualAccount

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