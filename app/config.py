"""Application configuration."""
import os

# SQLite database location. Inside the container /data is mounted as a volume.
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/hydroponic.db")

# Elements supported by the solver. N/P/K are the primary macronutrients;
# the rest are tracked micronutrients.
ELEMENTS = ["N", "P", "K", "Ca", "Mg", "S", "Fe", "Mn", "Zn", "B", "Cu", "Mo"]

# Relative deviation (fraction, e.g. 0.05 = 5%) above which a target element
# is reported as over/under in the trial result.
DEFAULT_TOLERANCE = float(os.getenv("DEFAULT_TOLERANCE", "0.05"))


def normalize_element(key: str) -> str | None:
    """Map a user-supplied element key to its canonical symbol.

    Accepts case-insensitive input ("n", "ca", "CA", "fe") and returns the
    symbol used in :data:`ELEMENTS`, or ``None`` if unknown.  ``capitalize()``
    is not used because it would corrupt two-letter symbols ("FE" -> "Fe"
    happens to work, but explicit matching is clearer for every symbol).
    """
    folded = key.strip().lower()
    for elem in ELEMENTS:
        if folded == elem.lower():
            return elem
    return None
