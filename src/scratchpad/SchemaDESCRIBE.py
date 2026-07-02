from pyopensky.trino import Trino

# pyopensky handles the credentials from your config automatically
trino = Trino()

# Trino().query() executes raw SQL and returns a pandas DataFrame
df = trino.query("DESCRIBE flights_data4")
print(df)