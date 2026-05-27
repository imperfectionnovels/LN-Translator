"""LN-Translator backend package."""

# Bumped per release. Read by routes/diagnostics.py for the About card on
# /settings. Keep in sync with pyproject.toml; we don't parse pyproject at
# request time because PyInstaller bundles do not ship pyproject.toml.
__version__ = "0.1.0"
