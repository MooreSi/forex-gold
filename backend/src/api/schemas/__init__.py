"""Pydantic response models.

One module per domain, named for the router that returns it. A schema's job is
to say what a field is called and what type it is — never to compute one.
A model with a computed field is logic that has moved into the API layer.
"""
