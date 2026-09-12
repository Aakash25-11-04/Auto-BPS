"""Layer 2 — Data Integration & Preprocessing.

An explicit, inspectable pipeline: ingestion -> validation -> cleaning ->
normalization -> asset/corridor ID mapping -> the unified database. Each
stage is its own module so the pipeline can be read top to bottom rather
than reverse-engineered from one big function.
"""
