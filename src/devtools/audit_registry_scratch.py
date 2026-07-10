"""
Standalone registry audit script - run from workspace root with:
  python -m scripts.audit_registry
or as a standalone by adjusting BASE_DIR below.
"""
import sys
import pandas as pd
import glob
from pathlib import Path

# Inject project root so src.* imports work when run directly
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import GLOBAL_CLEAN_REGISTRY, BASE_DIR

df = pd.read_parquet(GLOBAL_CLEAN_REGISTRY)

found = glob.glob(str(BASE_DIR / 'data/trajectories/**/*_clean_si.parquet'), recursive=True)
print('All *_clean_si.parquet on disk:', len(found))

indexed_fids = set(df['flight_id'].unique())
print('flight_ids in registry:', len(indexed_fids))

total_on_disk = 0
missing_from_reg = 0
for p in found:
    try:
        d = pd.read_parquet(p, columns=['flight_id'])
        fids = set(d['flight_id'].dropna().unique())
        total_on_disk += len(fids)
        missing = fids - indexed_fids
        missing_from_reg += len(missing)
        if missing:
            print(f'  UNINDEXED: {Path(p).name} missing {len(missing)} fids')
    except Exception as e:
        print(f'Error reading {p}: {e}')

print(f'Total flight_ids on disk: {total_on_disk}')
print(f'Missing from registry: {missing_from_reg}')

in_raw = df['file_path'].str.contains('/raw/').sum()
print(f'Stale entries pointing to /raw/ dirs: {in_raw}')
