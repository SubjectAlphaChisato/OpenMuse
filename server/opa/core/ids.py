from ulid import ULID


def new_id() -> str:
    return str(ULID())
