from pathlib import Path


class CatalogRepository:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.database.touch(exist_ok=True)
