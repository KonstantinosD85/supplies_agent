import pytest

from supplies_agent.memory import facts

pytestmark = pytest.mark.anyio


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "agent.db")
    facts.setup(path)
    return path


async def profile(store) -> list[str]:
    return [entry.content for entry in await store.search("anything")]


async def test_new_user_has_no_facts(db_path):
    assert await profile(facts.store(db_path, "alice")) == []


async def test_a_fact_is_one_value_per_key(db_path):
    store = facts.store(db_path, "alice")
    await store.add("Lab 3.12", {"key": "delivery_location"})
    await store.add("Lab 4.01", {"key": "delivery_location"})
    await store.add("Proteomics", {"key": "team"})

    # The whole profile, whatever the query, with the newer value replacing the older.
    assert await profile(store) == ["delivery_location: Lab 4.01", "team: Proteomics"]


async def test_facts_survive_a_new_store_on_the_same_database(db_path):
    await facts.store(db_path, "alice").add("Lab 3.12", {"key": "delivery_location"})
    assert await profile(facts.store(db_path, "alice")) == ["delivery_location: Lab 3.12"]


async def test_users_never_see_each_others_facts(db_path):
    await facts.store(db_path, "alice").add("Lab 3.12", {"key": "delivery_location"})
    await facts.store(db_path, "bob").add("Lab 9.99", {"key": "delivery_location"})

    assert await profile(facts.store(db_path, "alice")) == ["delivery_location: Lab 3.12"]
    assert await profile(facts.store(db_path, "bob")) == ["delivery_location: Lab 9.99"]


@pytest.mark.parametrize("metadata", [None, {}, {"key": "favourite_colour"}])
async def test_a_write_without_a_known_key_is_rejected(db_path, metadata):
    store = facts.store(db_path, "alice")
    with pytest.raises(ValueError, match="Use one of: lab, team, delivery_location"):
        await store.add("free-form note", metadata)
    assert await profile(store) == []


@pytest.mark.parametrize("value", ["", "   ", "x" * 101])
async def test_a_value_must_be_one_short_line(db_path, value):
    with pytest.raises(ValueError, match="1 to 100 characters"):
        await facts.store(db_path, "alice").add(value, {"key": "lab"})


async def test_values_are_kept_on_one_line(db_path):
    store = facts.store(db_path, "alice")
    await store.add("Lab 3.12\n  cold room", {"key": "delivery_location"})
    assert await profile(store) == ["delivery_location: Lab 3.12 cold room"]
