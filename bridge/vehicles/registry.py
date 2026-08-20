"""
Maps brand_id -> adapter class. Add one line here (and one new file in this
folder) to onboard a future brand - nothing else in the codebase changes.
"""

from .mg import MgAdapter
from .maxus import MaxusAdapter

ADAPTER_CLASSES = {
    "mg": MgAdapter,
    "maxus": MaxusAdapter,
}
