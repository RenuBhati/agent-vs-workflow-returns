"""The only code that writes to the database.

Both assistants reach the shop through these functions. Every function takes
a Ctx, which fixes the shopper for the whole session, so no caller can ask
for another shopper's data.
"""
