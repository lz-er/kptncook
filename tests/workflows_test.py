import copy
import logging
from types import SimpleNamespace

import httpx
import pytest

from kptncook.models import Recipe
from kptncook.repositories import RecipeInDb
from kptncook.services import repository as repository_service
from kptncook.services import workflows
from kptncook.services.repository import InvalidStoredRecipe, RepositoryRecipesResult


def _recipe_data(minimal, *, oid: str | None = None) -> dict:
    data = copy.deepcopy(minimal)
    if oid is not None:
        data["_id"]["$oid"] = oid
    return data


def _recipe(minimal, *, oid: str | None = None) -> Recipe:
    return Recipe.model_validate(_recipe_data(minimal, oid=oid))


def _recipe_in_db(minimal, *, oid: str | None = None) -> RecipeInDb:
    return RecipeInDb(date=workflows.date.today(), data=_recipe_data(minimal, oid=oid))


def _status_error(
    status_code: int, *, detail_message: str | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mealie.example.com/api/recipes")
    response_kwargs = {"request": request}
    if detail_message is not None:
        response_kwargs["json"] = {"detail": {"message": detail_message}}
    response = httpx.Response(status_code, **response_kwargs)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_save_todays_recipes_uses_repository_service(monkeypatch):
    recipe = RecipeInDb(
        date=workflows.date.today(),
        data={"_id": {"$oid": "635a68635100007500061cd7"}},
    )
    captured = {}

    monkeypatch.setattr(workflows, "repository_needs_sync", lambda _date: True)
    monkeypatch.setattr(workflows, "get_today_recipes", lambda: [recipe])

    def fake_save_recipe_entries(recipes):
        captured["recipes"] = recipes
        return len(recipes)

    monkeypatch.setattr(workflows, "save_recipe_entries", fake_save_recipe_entries)

    assert workflows.save_todays_recipes() == 1
    assert captured["recipes"] == [recipe]


def test_save_todays_recipes_skips_fetch_when_repository_is_current(monkeypatch):
    monkeypatch.setattr(workflows, "repository_needs_sync", lambda _date: False)
    monkeypatch.setattr(
        workflows,
        "get_today_recipes",
        lambda: pytest.fail("today recipes should not be fetched"),
    )
    monkeypatch.setattr(
        workflows,
        "save_recipe_entries",
        lambda recipes: pytest.fail(f"repository should not be written: {recipes}"),
    )

    assert workflows.save_todays_recipes() == 0


def test_save_todays_recipes_wraps_repository_errors(monkeypatch):
    def fail_needs_sync(_date):
        raise repository_service.RepositoryServiceError("repository unavailable")

    monkeypatch.setattr(workflows, "repository_needs_sync", fail_needs_sync)

    with pytest.raises(workflows.UserFacingError, match="repository unavailable"):
        workflows.save_todays_recipes()


def test_load_repository_recipes_reports_invalid_entries(monkeypatch, minimal):
    valid_entry = RecipeInDb(date=workflows.date.today(), data=minimal)
    invalid_data = _recipe_data(minimal, oid="broken")
    invalid_data.pop("steps")
    invalid_entry = RecipeInDb(
        date=workflows.date.today(),
        data=invalid_data,
    )
    expected_recipe = Recipe.model_validate(minimal)

    monkeypatch.setattr(
        repository_service,
        "list_repository_entries",
        lambda: [valid_entry, invalid_entry],
    )

    result = repository_service.load_repository_recipes()

    assert result.recipes == [expected_recipe]
    assert len(result.invalid_entries) == 1
    assert result.invalid_entries[0].recipe_id == "broken"
    assert result.invalid_entries[0].reason == "steps: Field required"


def test_sync_with_mealie_skips_duplicates_and_logs_other_failures(
    monkeypatch, minimal, caplog
):
    warning = InvalidStoredRecipe(
        position=2,
        recipe_id="broken",
        reason="steps: Field required",
    )
    repository_recipes = [
        _recipe(minimal, oid="recipe-1"),
        _recipe(minimal, oid="recipe-2"),
        _recipe(minimal, oid="recipe-3"),
    ]
    mealie_recipes = [
        SimpleNamespace(
            name=f"Minimal Recipe {index}",
            extras={"kptncook_id": recipe.id.oid},
        )
        for index, recipe in enumerate(repository_recipes, start=1)
    ]
    seen_ids: list[str] = []

    class FakeClient:
        def create_recipe(self, recipe_to_create):
            recipe_id = recipe_to_create.extras["kptncook_id"]
            seen_ids.append(recipe_id)
            if recipe_id == "recipe-1":
                raise _status_error(409, detail_message="Recipe already exists")
            if recipe_id == "recipe-2":
                raise _status_error(500, detail_message="upstream exploded")
            return SimpleNamespace(slug=f"created-{recipe_id}")

    fake_client = FakeClient()

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: fake_client)
    monkeypatch.setattr(
        workflows,
        "get_kptncook_recipes_from_mealie",
        lambda _client: [],
    )
    monkeypatch.setattr(
        workflows,
        "load_kptncook_recipes_from_repository",
        lambda: RepositoryRecipesResult(
            recipes=repository_recipes,
            invalid_entries=[warning],
        ),
    )
    monkeypatch.setattr(
        workflows,
        "kptncook_to_mealie",
        lambda recipe: mealie_recipes[repository_recipes.index(recipe)],
    )

    with caplog.at_level(logging.WARNING):
        result = workflows.sync_with_mealie_result()

    assert result.created_count == 1
    assert result.invalid_repository_entries == [warning]
    assert seen_ids == ["recipe-1", "recipe-2", "recipe-3"]
    assert "Minimal Recipe 2" in caplog.text
    assert "upstream exploded" in caplog.text
    assert "Recipe already exists" not in caplog.text


def test_sync_with_mealie_prefilters_recipes_already_present_in_mealie(
    monkeypatch, minimal
):
    repository_recipes = [
        _recipe(minimal, oid="recipe-1"),
        _recipe(minimal, oid="recipe-2"),
    ]
    mealie_recipes = [
        SimpleNamespace(
            name=f"Minimal Recipe {index}",
            extras={"kptncook_id": recipe.id.oid},
        )
        for index, recipe in enumerate(repository_recipes, start=1)
    ]
    seen_ids: list[str] = []

    class FakeClient:
        def create_recipe(self, recipe_to_create):
            recipe_id = recipe_to_create.extras["kptncook_id"]
            seen_ids.append(recipe_id)
            return SimpleNamespace(slug=f"created-{recipe_id}")

    fake_client = FakeClient()

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: fake_client)
    monkeypatch.setattr(
        workflows,
        "get_kptncook_recipes_from_mealie",
        lambda _client: [mealie_recipes[0]],
    )
    monkeypatch.setattr(
        workflows,
        "load_kptncook_recipes_from_repository",
        lambda: RepositoryRecipesResult(
            recipes=repository_recipes,
            invalid_entries=[],
        ),
    )
    monkeypatch.setattr(
        workflows,
        "kptncook_to_mealie",
        lambda recipe: mealie_recipes[repository_recipes.index(recipe)],
    )

    result = workflows.sync_with_mealie_result()

    assert result.created_count == 1
    assert result.invalid_repository_entries == []
    assert seen_ids == ["recipe-2"]


def test_backup_kptncook_favorites_resolves_and_saves_recipes(monkeypatch, minimal):
    expected_recipes = [
        _recipe_in_db(minimal, oid="favorite-1"),
        _recipe_in_db(minimal, oid="favorite-2"),
    ]
    favorites = [{"id": "favorite-1"}, {"id": "favorite-2"}, {"id": "favorite-3"}]
    captured: dict[str, object] = {}

    class FakeClient:
        def list_favorites(self):
            return favorites

    fake_client = FakeClient()

    monkeypatch.setattr(
        workflows,
        "_require_access_token",
        lambda: captured.setdefault("required", True),
    )
    monkeypatch.setattr(workflows, "KptnCookClient", lambda: fake_client)
    monkeypatch.setattr(
        workflows,
        "_collect_recipe_identifiers",
        lambda items: [("oid", item["id"]) for item in items[:2]],
    )

    def fake_resolve_recipe_summaries(client, items, *, action):
        captured["client"] = client
        captured["items"] = items
        captured["action"] = action
        return expected_recipes

    monkeypatch.setattr(
        workflows, "_resolve_recipe_summaries", fake_resolve_recipe_summaries
    )
    monkeypatch.setattr(
        workflows,
        "_save_repository_entries",
        lambda recipes: len(recipes),
    )

    result = workflows.backup_kptncook_favorites()

    assert result == workflows.FavoritesBackupResult(favorite_count=3, saved_count=2)
    assert captured["required"] is True
    assert captured["client"] is fake_client
    assert captured["items"] == [("oid", "favorite-1"), ("oid", "favorite-2")]
    assert captured["action"] == "resolving recipes"


def test_get_discovery_list_recipes_resolves_list_items(monkeypatch, minimal):
    expected_recipes = [_recipe_in_db(minimal, oid="discovery-1")]
    captured: dict[str, object] = {}

    class FakeClient:
        def get_discovery_list(self, *, list_type, list_id):
            captured["list_type"] = list_type
            captured["list_id"] = list_id
            return [{"id": "discovery-1"}]

    fake_client = FakeClient()
    monkeypatch.setattr(workflows, "KptnCookClient", lambda: fake_client)

    def fake_resolve_recipe_summaries(client, items, *, action):
        captured["client"] = client
        captured["items"] = items
        captured["action"] = action
        return expected_recipes

    monkeypatch.setattr(
        workflows, "_resolve_recipe_summaries", fake_resolve_recipe_summaries
    )

    result = workflows.get_discovery_list_recipes(list_type="curated", list_id="abc123")

    assert result == expected_recipes
    assert captured["list_type"] == "curated"
    assert captured["list_id"] == "abc123"
    assert captured["client"] is fake_client
    assert captured["items"] == [{"id": "discovery-1"}]
    assert captured["action"] == "resolving recipes"


def test_get_recipes_with_ingredients_requires_access_token_and_resolves_items(
    monkeypatch, minimal
):
    expected_recipes = [_recipe_in_db(minimal, oid="ingredient-1")]
    captured: dict[str, object] = {}

    class FakeClient:
        def get_recipes_with_ingredients(self, *, ingredient_ids):
            captured["ingredient_ids"] = ingredient_ids
            return [{"id": "ingredient-1"}]

    fake_client = FakeClient()
    monkeypatch.setattr(
        workflows,
        "_require_access_token",
        lambda: captured.setdefault("required", True),
    )
    monkeypatch.setattr(workflows, "KptnCookClient", lambda: fake_client)

    def fake_resolve_recipe_summaries(client, items, *, action):
        captured["client"] = client
        captured["items"] = items
        captured["action"] = action
        return expected_recipes

    monkeypatch.setattr(
        workflows, "_resolve_recipe_summaries", fake_resolve_recipe_summaries
    )

    result = workflows.get_recipes_with_ingredients(["123", "456"])

    assert result == expected_recipes
    assert captured["required"] is True
    assert captured["ingredient_ids"] == ["123", "456"]
    assert captured["client"] is fake_client
    assert captured["items"] == [{"id": "ingredient-1"}]
    assert captured["action"] == "resolving recipes"


def test_get_onboarding_recipes_resolves_items(monkeypatch, minimal):
    expected_recipes = [_recipe_in_db(minimal, oid="onboarding-1")]
    captured: dict[str, object] = {}

    class FakeClient:
        def get_onboarding_recipes(self, *, tags):
            captured["tags"] = tags
            return [{"id": "onboarding-1"}]

    fake_client = FakeClient()
    monkeypatch.setattr(workflows, "KptnCookClient", lambda: fake_client)

    def fake_resolve_recipe_summaries(client, items, *, action):
        captured["client"] = client
        captured["items"] = items
        captured["action"] = action
        return expected_recipes

    monkeypatch.setattr(
        workflows, "_resolve_recipe_summaries", fake_resolve_recipe_summaries
    )

    result = workflows.get_onboarding_recipes(["rt:diet_vegetarian"])

    assert result == expected_recipes
    assert captured["tags"] == ["rt:diet_vegetarian"]
    assert captured["client"] is fake_client
    assert captured["items"] == [{"id": "onboarding-1"}]
    assert captured["action"] == "resolving recipes"


def test_delete_recipes_by_selection_deduplicates_ids_and_reports_missing(
    monkeypatch, minimal
):
    warning = InvalidStoredRecipe(
        position=3,
        recipe_id="broken",
        reason="ingredients: Field required",
    )
    recipes = [
        _recipe(minimal, oid="recipe-1"),
        _recipe(minimal, oid="recipe-2"),
    ]
    monkeypatch.setattr(
        workflows,
        "load_kptncook_recipes_from_repository",
        lambda: RepositoryRecipesResult(recipes=recipes, invalid_entries=[warning]),
    )
    monkeypatch.setattr(
        workflows,
        "_repository_id_map",
        lambda: {
            "recipe-1": _recipe_in_db(minimal, oid="recipe-1"),
        },
    )

    result = workflows.delete_recipes_by_selection(
        indices=[0, -1, 3],
        oids=["recipe-1", "missing"],
    )

    assert result.recipes == recipes
    assert result.invalid_indices == [-1, 3]
    assert result.missing_ids == ["missing"]
    assert result.to_delete_ids == ["recipe-1"]
    assert result.invalid_repository_entries == [warning]


def test_export_recipes_to_paprika_accepts_parseable_repository_url(
    monkeypatch, minimal
):
    recipe = Recipe.model_validate(minimal)

    monkeypatch.setattr(
        workflows,
        "load_kptncook_recipes_from_repository",
        lambda: RepositoryRecipesResult(
            recipes=[recipe],
            invalid_entries=[],
        ),
    )
    monkeypatch.setattr(
        workflows.PaprikaExporter,
        "export",
        lambda self, recipes: "/tmp/export.paprikarecipes",
    )

    result = workflows.export_recipes_to_paprika_result(
        f"https://share.kptncook.com/{recipe.id.oid}"
    )

    assert result.filename == "/tmp/export.paprikarecipes"
    assert result.invalid_repository_entries == []


def test_get_discovery_screen_returns_structured_entries(monkeypatch):
    monkeypatch.setattr(
        workflows.KptnCookClient,
        "get_discovery_screen",
        lambda self: {
            "lists": [
                {
                    "id": "abc123",
                    "title": {"de": "Trending"},
                    "listType": "curated",
                }
            ],
            "quickSearchEntries": [{"title": "Winter"}],
        },
    )

    data = workflows.get_discovery_screen()

    assert len(data.lists) == 1
    assert data.lists[0].list_id == "abc123"
    assert data.lists[0].title == "Trending"
    assert data.lists[0].list_type == "curated"
    assert data.quick_search == ["Winter"]


def test_find_numbered_duplicates_only_when_base_name_exists():
    recipes = [
        SimpleNamespace(name="Pasta", slug="pasta"),
        SimpleNamespace(name="Pasta (1)", slug="pasta-1"),
        SimpleNamespace(name="Pasta (2)", slug="pasta-2"),
        # base "Salad" is missing, so "Salad (1)" is kept
        SimpleNamespace(name="Salad (1)", slug="salad-1"),
        SimpleNamespace(name="Soup", slug="soup"),
    ]

    duplicates = workflows._find_numbered_duplicates(recipes)

    assert [d.slug for d in duplicates] == ["pasta-1", "pasta-2"]


def test_find_numbered_duplicates_ignores_stray_whitespace():
    # Mealie sometimes stores a trailing space on the base and doubles the
    # space before the "(N)" suffix; both should still be matched.
    recipes = [
        SimpleNamespace(name="Salat mit Bohnen ", slug="salat"),
        SimpleNamespace(name="Salat mit Bohnen  (1)", slug="salat-1"),
    ]

    duplicates = workflows._find_numbered_duplicates(recipes)

    assert [d.slug for d in duplicates] == ["salat-1"]


def test_find_mealie_duplicates_uses_client(monkeypatch):
    recipes = [
        SimpleNamespace(name="Pizza", slug="pizza"),
        SimpleNamespace(name="Pizza (1)", slug="pizza-1"),
    ]

    class FakeClient:
        def get_all_recipes(self):
            return recipes

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: FakeClient())

    duplicates = workflows.find_mealie_duplicates()

    assert [d.slug for d in duplicates] == ["pizza-1"]


def test_delete_mealie_duplicates_reports_deleted_and_failed(monkeypatch):
    deleted_slugs: list[str] = []

    class FakeClient:
        def delete_via_slug(self, slug):
            if slug == "boom-1":
                raise httpx.HTTPError("nope")
            deleted_slugs.append(slug)
            return {}

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: FakeClient())

    duplicates = [
        workflows.MealieRecipeRef(name="Ok (1)", slug="ok-1"),
        workflows.MealieRecipeRef(name="Boom (1)", slug="boom-1"),
    ]

    result = workflows.delete_mealie_duplicates(duplicates)

    assert deleted_slugs == ["ok-1"]
    assert [d.slug for d in result.deleted] == ["ok-1"]
    assert [d.slug for d, _ in result.failed] == ["boom-1"]


def test_is_empty_recipe_uses_raw_camelcase_keys():
    assert workflows._is_empty_recipe({"recipeInstructions": [], "recipeIngredient": []})
    assert workflows._is_empty_recipe({})
    assert not workflows._is_empty_recipe(
        {"recipeInstructions": [{"text": "step"}], "recipeIngredient": []}
    )
    assert not workflows._is_empty_recipe(
        {"recipeInstructions": [], "recipeIngredient": [{"note": "1 Cup Flour"}]}
    )


def test_find_mealie_empty_recipes_detects_only_empty(monkeypatch):
    summaries = [
        SimpleNamespace(name="Real", slug="real"),
        SimpleNamespace(name="Empty", slug="empty"),
    ]
    details = {
        "real": {
            "name": "Real",
            "recipeInstructions": [{"text": "cook"}],
            "recipeIngredient": [{"note": "egg"}],
        },
        "empty": {"name": "Empty", "recipeInstructions": [], "recipeIngredient": []},
    }

    class FakeClient:
        def get_all_recipes(self):
            return summaries

        def get_recipe_dict(self, slug):
            return details[slug]

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: FakeClient())

    empty = workflows.find_mealie_empty_recipes()

    assert [ref.slug for ref in empty] == ["empty"]


def test_is_broken_recipe_matches_placeholder_or_missing_steps():
    # No steps at all -> broken.
    assert workflows._is_broken_recipe({"recipeInstructions": []})
    # Single default placeholder step -> broken (even with real ingredients).
    assert workflows._is_broken_recipe(
        {
            "recipeInstructions": [
                {"text": workflows._DEFAULT_STEP_PREFIX + "\n\n**Add a link**"}
            ],
            "recipeIngredient": [{"note": "Pfeffer"}],
        }
    )
    # A real single step -> not broken.
    assert not workflows._is_broken_recipe(
        {"recipeInstructions": [{"text": "Chop the onion."}]}
    )
    # Multiple steps -> not broken.
    assert not workflows._is_broken_recipe(
        {"recipeInstructions": [{"text": "a"}, {"text": "b"}]}
    )


def test_repair_mealie_recipes_deletes_and_recreates_matched(monkeypatch, minimal):
    broken = [
        workflows.MealieRecipeRef(name="Minimal Recipe", slug="minimal-recipe"),
        workflows.MealieRecipeRef(name="Unknown Recipe", slug="unknown-recipe"),
    ]
    source = workflows.kptncook_to_mealie(_recipe(minimal))

    deleted: list[str] = []
    created: list[str] = []

    class FakeClient:
        def delete_via_slug(self, slug):
            deleted.append(slug)
            return {}

        def create_recipe(self, recipe):
            created.append(recipe.name)
            return SimpleNamespace(slug="new-slug")

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: FakeClient())
    monkeypatch.setattr(
        workflows,
        "_repository_recipes_by_name",
        lambda: {workflows._normalize_recipe_name(source.name): source},
    )

    # Align the first broken recipe's name with the repository source name.
    broken[0] = workflows.MealieRecipeRef(name=source.name, slug="minimal-recipe")

    result = workflows.repair_mealie_recipes(broken)

    assert deleted == ["minimal-recipe"]
    assert created == [source.name]
    assert [ref.slug for ref in result.repaired] == ["minimal-recipe"]
    assert [ref.slug for ref in result.unmatched] == ["unknown-recipe"]
    assert result.failed == []


def test_create_mealie_cookbooks_creates_updates_and_skips(monkeypatch):
    tags = [
        {"name": "diet_vegetarian", "slug": "diet-vegetarian", "id": "veg"},
        {"name": "main_dish", "slug": "main-dish", "id": "main"},
        {"name": "peanut_free", "slug": "peanut-free", "id": "pf"},
        {"name": "cooking_time_under_20", "slug": "cooking-time-under-20", "id": "u20"},
    ]
    created: list[dict] = []
    updated: list[tuple] = []

    class FakeClient:
        def get_all_tags(self):
            return tags

        def get_cookbooks(self):
            return [{"id": "cb1", "name": "Diet Vegetarian", "slug": "diet-vegetarian"}]

        def create_cookbook(self, payload):
            created.append(payload)
            return {**payload, "id": "new"}

        def update_cookbook(self, cookbook_id, payload):
            updated.append((cookbook_id, payload))
            return payload

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: FakeClient())

    result = workflows.create_mealie_cookbooks(
        category_tags=["cooking_time_under_20", "diet_vegetarian", "unknown_tag"],
        require_tags=["main_dish", "peanut_free"],
    )

    assert result.created == ["Cooking Time Under 20"]
    assert result.updated == ["Diet Vegetarian"]
    assert result.missing_tags == ["unknown_tag"]
    qfs = created[0]["queryFilterString"]
    assert qfs.startswith("tags.id CONTAINS ALL [")
    assert '"u20"' in qfs and '"main"' in qfs and '"pf"' in qfs
    assert updated[0][0] == "cb1"


def test_create_mealie_cookbooks_errors_on_missing_required_tag(monkeypatch):
    class FakeClient:
        def get_all_tags(self):
            return [{"name": "diet_vegetarian", "slug": "diet-vegetarian", "id": "veg"}]

        def get_cookbooks(self):
            return []

    monkeypatch.setattr(workflows, "get_mealie_client", lambda: FakeClient())

    with pytest.raises(workflows.UserFacingError):
        workflows.create_mealie_cookbooks(
            category_tags=["diet_vegetarian"], require_tags=["main_dish"]
        )


def test_plan_categorization_matches_rules_sources_and_tools():
    records = [
        # slug, source, tags, existing tools
        ("veg-main", "kptncook",
         {"diet_vegetarian", "main_dish", "peanut_free"}, set()),
        ("veg-not-main", "kptncook", {"diet_vegetarian", "peanut_free"}, set()),
        ("one-pot", "kptncook", {"one_pot", "main_dish"}, set()),
        ("already-tooled", "kptncook", {"one_pot"}, {"One Pot"}),
        ("manual", None, {"diet_vegetarian", "main_dish", "peanut_free"}, set()),
    ]
    rules = (
        workflows.CategoryRule(
            ("diet_vegetarian", "main_dish", "peanut_free"), "main_vegetarian"
        ),
    )
    kptncook_slugs, rule_slugs, tool_assignments = workflows._plan_categorization(
        records, rules, {"one_pot": "One Pot"}, add_tools=True
    )

    # only kptncook-source recipes get the kptncook category
    assert kptncook_slugs == ["veg-main", "veg-not-main", "one-pot", "already-tooled"]
    # rule requires ALL three tags (manual has them too, regardless of source)
    assert rule_slugs["main_vegetarian"] == ["veg-main", "manual"]
    # only recipes missing the derived tool get an assignment
    assert tool_assignments == {"one-pot": {"One Pot"}}


def test_plan_categorization_can_skip_tools():
    records = [("one-pot", "kptncook", {"one_pot"}, set())]
    _, _, tool_assignments = workflows._plan_categorization(
        records, (), {"one_pot": "One Pot"}, add_tools=False
    )
    assert tool_assignments == {}



