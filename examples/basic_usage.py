"""Upload sample text files to WaveflowDB and search them.

First:
    Install: `pip install waveflowdb-client`
    Create a namespace and generate API credentials in the WaveflowDB UI.
    Configure `API_KEY` and `USER_ID` below before running.
"""

from waveflowdb_client import VectorLake
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

API_KEY = os.getenv("WAVEFLOW_API_KEY")
USER_ID = os.getenv("WAVEFLOW_USER_ID")

# Initialize the client with your API key and email
client = VectorLake(
    api_key = API_KEY,
    user_id = USER_ID,
    host = "https://waveflow-analytics.com",
    vector_lake_path = "./upload"  # Local folder containing your files
)

# Connect to an existing namespace created in the UI
index = client.Index("test_namespace")

# Upload files from the configured folder
index.add(files=["faq1.txt", "policy.txt"])

#Search for relevant information
results = index.query("refund policy", top_k=5)

print(results)