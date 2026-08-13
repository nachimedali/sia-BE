"""Analytics services (design.md §8.9).

`ingest` fills the tables, `signals` reads them, `repurposing` acts on what
they say, `sentiment` labels the comments. Callers import the module they need
(`from analytics.services import signals`) and go through it; nothing reaches
past these into the models.
"""
