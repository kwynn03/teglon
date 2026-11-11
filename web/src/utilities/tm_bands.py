"""Treasure Map band name -> Teglon `Band.Name`.

Kept dependency-free so both the loader (load_observed_tiles.py) and the cron sync (get_TM_pointings.py)
agree on which pointings are loadable. Adjust the right-hand side to match the names in your Teglon
`Band` table. A TM band that isn't listed is used as-is (useful if your Band table already uses
TM-style names).
"""

TM_TO_TEGLON_BAND = {
    "U": "Landolt U",
    "B": "Landolt B",
    "V": "Landolt V",
    "R": "Landolt R",
    "I": "Landolt I",
    "J": "UKIRT J",
    "H": "UKIRT H",
    "K": "UKIRT K",
    "u": "SDSS u",
    "g": "SDSS g",
    "r": "SDSS r",
    "i": "SDSS i",
    "z": "SDSS z",
    "clear": "Clear",
    "open": "Clear",
}


# m_AB - m_Vega per Teglon band, used to convert Treasure Map depths reported in Vega mag to AB.
#
# Source: Blanton, M. R. & Roweis, S. 2007, "K-Corrections and Filter Transformations in the Ultraviolet,
#   Optical, and Near-Infrared", AJ, 133, 734, Table 1 (doi:10.1086/510127).
#
# Caveats (approximations made here, not in the paper):
#   - U/B/V/R/I are the paper's Bessell (Johnson-Cousins) filters; Teglon names them "Landolt".
#   - J/H/K are the paper's 2MASS J/H/Ks values, applied to Teglon's "UKIRT" J/H/K bands.
#   - "Clear" (unfiltered, e.g. MASTER) has no published offset; it is approximated as Cousins R.
VEGA_TO_AB = {
    "Landolt U": 0.79,
    "Landolt B": -0.09,
    "Landolt V": 0.02,
    "Landolt R": 0.21,
    "Landolt I": 0.45,
    "UKIRT J": 0.91,
    "UKIRT H": 1.39,
    "UKIRT K": 1.85,
    "SDSS u": 0.91,
    "SDSS g": -0.08,
    "SDSS r": 0.16,
    "SDSS i": 0.37,
    "SDSS z": 0.54,
    "Clear": 0.21,
}


def vega_to_ab_offset(teglon_band):
    """m_AB - m_Vega for a Teglon band name, or None if there's no known offset."""
    return VEGA_TO_AB.get(teglon_band)


def teglon_band_name(tm_band):
    """Teglon Band.Name for a Treasure Map band (handles enum reprs like `bandpass.r`)."""
    tm_band = str(tm_band or "").split(".")[-1]
    return TM_TO_TEGLON_BAND.get(tm_band, tm_band)
