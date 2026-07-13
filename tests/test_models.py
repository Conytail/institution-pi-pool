from pi_index.models import InstitutionRecord, stable_id


def test_stable_id_is_stable():
    assert stable_id("x", "a", "b") == stable_id("x", "a", "b")
    assert stable_id("x", "a", "b").startswith("x_")


def test_institution_record_json():
    record = InstitutionRecord(
        institution_id="inst_1",
        name="Example University",
        official_domains=["example.edu"],
    )
    data = record.to_dict()
    assert data["name"] == "Example University"
    assert data["official_domains"] == ["example.edu"]
