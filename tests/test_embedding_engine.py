"""Tests for services/registry/embedding_engine.py – EmbeddingEngine."""
import pytest
from unittest.mock import MagicMock

from services.registry.embedding_engine import EmbeddingEngine
from services.tool_contracts import ParameterDefinition, DependencySource


@pytest.fixture
def engine():
    return EmbeddingEngine()


def _param(name, context_key=None, source=DependencySource.FROM_USER):
    return ParameterDefinition(name=name, context_key=context_key, source=source)


# ===========================================================================
# _generate_purpose
# ===========================================================================

class TestGeneratePurpose:
    def test_get_method(self, engine):
        purpose = engine._generate_purpose("GET", {}, [])
        assert "Dohvaća" in purpose

    def test_post_method(self, engine):
        purpose = engine._generate_purpose("POST", {}, [])
        assert "Kreira" in purpose

    def test_put_method(self, engine):
        purpose = engine._generate_purpose("PUT", {}, [])
        assert "Ažurira" in purpose

    def test_delete_method(self, engine):
        purpose = engine._generate_purpose("DELETE", {}, [])
        assert "Briše" in purpose

    def test_unknown_method(self, engine):
        purpose = engine._generate_purpose("HEAD", {}, [])
        assert "Obrađuje" in purpose

    def test_vehicle_context(self, engine):
        params = {"VehicleId": _param("VehicleId")}
        purpose = engine._generate_purpose("GET", params, [])
        # Uses genitive form "vozila" (za vozila)
        assert "vozil" in purpose.lower()  # Matches vozilo, vozila

    def test_person_context(self, engine):
        params = {"PersonId": _param("PersonId")}
        purpose = engine._generate_purpose("GET", params, [])
        # Maps PersonId to osoba (not korisnik)
        assert "osob" in purpose.lower()  # Matches osoba, osobe

    def test_mileage_output(self, engine):
        purpose = engine._generate_purpose("GET", {}, ["Mileage", "LastMileage"])
        assert "kilometražu" in purpose

    def test_registration_output(self, engine):
        purpose = engine._generate_purpose("GET", {}, ["LicencePlate", "Registration"])
        assert "registraciju" in purpose

    def test_expiration_output(self, engine):
        purpose = engine._generate_purpose("GET", {}, ["ExpirationDate"])
        # ExpirationDate maps to "datum isteka"
        assert "datum isteka" in purpose

    def test_status_output(self, engine):
        purpose = engine._generate_purpose("GET", {}, ["Status"])
        assert "status" in purpose

    def test_available_output(self, engine):
        purpose = engine._generate_purpose("GET", {}, ["AvailableVehicles"])
        assert "dostupnost" in purpose

    def test_time_period(self, engine):
        params = {
            "FromTime": _param("FromTime"),
            "ToTime": _param("ToTime"),
        }
        purpose = engine._generate_purpose("GET", params, [])
        assert "periodu" in purpose

    def test_booking_context(self, engine):
        params = {"BookingId": _param("BookingId")}
        purpose = engine._generate_purpose("GET", params, [])
        # Uses genitive form "rezervacije" (za rezervacije)
        assert "rezervacij" in purpose.lower()  # Matches rezervacija, rezervacije

    def test_name_output(self, engine):
        purpose = engine._generate_purpose("GET", {}, ["FullVehicleName"])
        assert "naziv" in purpose


# ===========================================================================
# build_embedding_text
# ===========================================================================

class TestBuildEmbeddingText:
    def test_basic(self, engine):
        text = engine.build_embedding_text(
            "get_MasterData", "VehicleService", "/api/md", "GET",
            "Get master data", {}, ["Mileage", "LicencePlate"]
        )
        assert "Returns" in text
        assert "Mileage" in text

    def test_truncation(self, engine):
        long_desc = "x" * 2000
        text = engine.build_embedding_text(
            "op", "svc", "/api", "GET", long_desc, {}, []
        )
        assert len(text) <= 1500

    def test_no_output_keys(self, engine):
        text = engine.build_embedding_text(
            "op", "svc", "/api", "GET", "desc", {}, None
        )
        assert "Returns" not in text

    def test_camel_case_split(self, engine):
        text = engine.build_embedding_text(
            "op", "svc", "/api", "GET", "", {}, ["FullVehicleName"]
        )
        assert "Full Vehicle Name" in text


# ===========================================================================
# build_dependency_graph
# ===========================================================================

class TestBuildDependencyGraph:
    def test_no_dependencies(self, engine):
        tool = MagicMock()
        tool.get_output_params.return_value = {}
        graph = engine.build_dependency_graph({"t1": tool})
        assert len(graph) == 0

    def test_with_dependency(self, engine):
        # t2 needs VehicleId, t1 provides it
        t1 = MagicMock()
        t1.get_output_params.return_value = {}
        t1.output_keys = ["VehicleId", "Name"]

        t2 = MagicMock()
        t2.get_output_params.return_value = {"VehicleId": _param("VehicleId")}
        t2.output_keys = []

        graph = engine.build_dependency_graph({"t1": t1, "t2": t2})
        assert "t2" in graph
        assert "t1" in graph["t2"].provider_tools

    def test_case_insensitive_match(self, engine):
        t1 = MagicMock()
        t1.get_output_params.return_value = {}
        t1.output_keys = ["vehicleid"]

        t2 = MagicMock()
        t2.get_output_params.return_value = {"VehicleId": _param("VehicleId")}
        t2.output_keys = []

        graph = engine.build_dependency_graph({"t1": t1, "t2": t2})
        assert "t2" in graph


# ===========================================================================
# _find_providers
# ===========================================================================

class TestFindProviders:
    def test_exact_match(self, engine):
        t1 = MagicMock()
        t1.output_keys = ["VehicleId"]
        providers = engine._find_providers("VehicleId", {"t1": t1})
        assert "t1" in providers

    def test_case_insensitive(self, engine):
        t1 = MagicMock()
        t1.output_keys = ["vehicleid"]
        providers = engine._find_providers("VehicleId", {"t1": t1})
        assert "t1" in providers

    def test_no_match(self, engine):
        t1 = MagicMock()
        t1.output_keys = ["PersonId"]
        providers = engine._find_providers("VehicleId", {"t1": t1})
        assert len(providers) == 0
