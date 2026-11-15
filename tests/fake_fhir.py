"""In-memory stand-in for FhirClient so pipeline tests need no server."""

from typing import Any

from interoperability_pipeline.fhir.client import FhirWriteResult


class InMemoryFhir:
    """Stores resources by identifier, like HAPI's conditional update does."""

    def __init__(self) -> None:
        self.resources: list[dict[str, Any]] = []

    @staticmethod
    def _matches(resource: dict[str, Any], search_params: dict[str, str]) -> bool:
        token = search_params.get("identifier")
        if token is None:
            return True
        system, _, value = token.partition("|")
        return any(
            i.get("system") == system and i.get("value") == value
            for i in resource.get("identifier", [])
        )

    def conditional_update(
        self, resource_type: str, search_params: dict[str, str], resource: dict[str, Any]
    ) -> FhirWriteResult:
        for index, stored in enumerate(self.resources):
            if stored["resourceType"] == resource_type and self._matches(stored, search_params):
                self.resources[index] = {**resource, "id": stored["id"]}
                return FhirWriteResult(resource_type, stored["id"], created=False)
        new_id = str(1000 + len(self.resources))
        self.resources.append({**resource, "id": new_id})
        return FhirWriteResult(resource_type, new_id, created=True)

    def search(self, resource_type: str, search_params: dict[str, str]) -> list[dict[str, Any]]:
        return [
            r
            for r in self.resources
            if r["resourceType"] == resource_type and self._matches(r, search_params)
        ]

    def find_id(self, resource_type: str, search_params: dict[str, str]) -> str | None:
        found = self.search(resource_type, search_params)
        return found[0]["id"] if found else None

    def one(self, resource_type: str) -> dict[str, Any]:
        found = [r for r in self.resources if r["resourceType"] == resource_type]
        assert len(found) == 1, f"expected exactly one {resource_type}, found {len(found)}"
        return found[0]
