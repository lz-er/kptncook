from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError
from rich.progress import track as _rich_track

from kptncook.api import KptnCookClient, _collect_recipe_identifiers, parse_id
from kptncook.config import get_settings
from kptncook.env import ENV_PATH
from kptncook.http_errors import (
    UserFacingError,
    extract_mealie_detail_message,
    format_http_status_error,
    format_request_error,
)
from kptncook.markdown_exporter import MarkdownExporter
from kptncook.mealie import MealieApiClient, kptncook_to_mealie
from kptncook.models import Recipe
from kptncook.paprika import PaprikaExporter
from kptncook.password_manager import get_credentials
from kptncook.repositories import RecipeInDb
from kptncook.services.discovery import DiscoveryScreenData, parse_discovery_screen
from kptncook.services.repository import (
    InvalidStoredRecipe,
    RepositoryRecipesResult,
    RepositoryServiceError,
    delete_recipe_ids,
    load_repository_recipes,
    list_repository_ids,
    repository_needs_sync,
    save_recipe_entries,
)
from kptncook.tandoor import TandoorExporter

logger = logging.getLogger(__name__)
SHARE_URL_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_T = TypeVar("_T")


def track(
    iterable: Iterable[_T], *, description: str, total: int | None = None
) -> Iterator[_T]:
    """Iterate with progress feedback.

    Uses rich's animated bar on a TTY; in non-interactive contexts (Docker
    logs, piped output) it emits periodic plain-text lines instead, since the
    animated bar renders nothing useful there.
    """
    if total is None:
        try:
            total = len(iterable)  # type: ignore[arg-type]
        except TypeError:
            total = None
    if sys.stdout.isatty():
        yield from _rich_track(iterable, description=description, total=total)
        return
    count = 0
    step = max(1, (total or 200) // 10)
    for item in iterable:
        yield item
        count += 1
        if total is not None and (count % step == 0 or count == total):
            print(f"{description}: {count}/{total}", flush=True)
    if total is None:
        print(f"{description}: {count} done", flush=True)



@dataclass(frozen=True)
class FavoritesBackupResult:
    favorite_count: int
    saved_count: int


@dataclass(frozen=True)
class SearchResult:
    id_type: str
    id_value: str
    recipe: RecipeInDb


@dataclass(frozen=True)
class SyncWithMealieResult:
    created_count: int
    skipped_count: int
    repository_count: int
    invalid_repository_entries: list[InvalidStoredRecipe]


@dataclass(frozen=True)
class DailiesSyncResult:
    saved_count: int
    created_count: int
    skipped_count: int
    invalid_count: int


@dataclass(frozen=True)
class MealieRecipeRef:
    name: str
    slug: str


@dataclass(frozen=True)
class MealieDeleteResult:
    deleted: list[MealieRecipeRef]
    failed: list[tuple[MealieRecipeRef, str]]


@dataclass(frozen=True)
class MealieRepairResult:
    repaired: list[MealieRecipeRef]
    unmatched: list[MealieRecipeRef]
    failed: list[tuple[MealieRecipeRef, str]]


@dataclass(frozen=True)
class CookbookSyncResult:
    created: list[str]
    updated: list[str]
    missing_tags: list[str]


@dataclass(frozen=True)
class CategoryRule:
    require_tags: tuple[str, ...]
    category: str


@dataclass(frozen=True)
class CategorizeResult:
    scanned: int
    kptncook_count: int
    rule_counts: dict[str, int]
    tool_counts: dict[str, int]
    cookbooks_updated: list[str]


@dataclass(frozen=True)
class DeleteSelectionResult:
    recipes: list[Recipe]
    invalid_indices: list[int]
    missing_ids: list[str]
    to_delete_ids: list[str]
    invalid_repository_entries: list[InvalidStoredRecipe]


@dataclass(frozen=True)
class PaprikaExportResult:
    filename: str
    invalid_repository_entries: list[InvalidStoredRecipe]


@dataclass(frozen=True)
class TandoorExportResult:
    filenames: list[str]
    invalid_repository_entries: list[InvalidStoredRecipe]


@dataclass(frozen=True)
class MarkdownExportResult:
    filenames: list[str]
    invalid_repository_entries: list[InvalidStoredRecipe]


def _wrap_repository_error(exc: RepositoryServiceError) -> UserFacingError:
    return UserFacingError(str(exc))


def load_kptncook_recipes_from_repository() -> RepositoryRecipesResult:
    try:
        return load_repository_recipes()
    except RepositoryServiceError as exc:
        raise _wrap_repository_error(exc) from exc


def load_recipe_from_repository_by_oid(oid: str) -> RepositoryRecipesResult:
    result = load_kptncook_recipes_from_repository()
    return RepositoryRecipesResult(
        recipes=[recipe for recipe in result.recipes if recipe.id.oid == oid],
        invalid_entries=result.invalid_entries,
    )


def load_recipe_from_repository_by_id(id_: str) -> RepositoryRecipesResult:
    parsed = parse_id(id_)
    if parsed is None:
        raise UserFacingError("Could not parse id")
    _, id_value = parsed
    return load_recipe_from_repository_by_oid(id_value)


def _repository_id_map() -> dict[object, RecipeInDb]:
    try:
        return list_repository_ids()
    except RepositoryServiceError as exc:
        raise _wrap_repository_error(exc) from exc


def _save_repository_entries(recipes: list[RecipeInDb]) -> int:
    try:
        return save_recipe_entries(recipes)
    except RepositoryServiceError as exc:
        raise _wrap_repository_error(exc) from exc


def _delete_repository_ids(ids: list[str]) -> tuple[list[str], list[str]]:
    try:
        return delete_recipe_ids(ids)
    except RepositoryServiceError as exc:
        raise _wrap_repository_error(exc) from exc


def get_today_recipes() -> list[RecipeInDb]:
    return KptnCookClient().list_today()


def save_todays_recipes() -> int:
    try:
        if not repository_needs_sync(date.today()):
            return 0
        recipes = get_today_recipes()
        return save_recipe_entries(recipes)
    except RepositoryServiceError as exc:
        raise _wrap_repository_error(exc) from exc


def get_mealie_client() -> MealieApiClient:
    settings = get_settings()
    client = MealieApiClient(str(settings.mealie_url))
    try:
        if settings.mealie_api_token:
            client.login_with_token(settings.mealie_api_token)
            return client
        if settings.mealie_username and settings.mealie_password:
            client.login(settings.mealie_username, settings.mealie_password)
            return client
    except Exception as exc:
        raise UserFacingError(f"Could not login to mealie: {exc}") from exc
    raise UserFacingError(
        "Mealie authentication required. "
        "Set MEALIE_API_TOKEN or MEALIE_USERNAME/MEALIE_PASSWORD."
    )


def get_kptncook_recipes_from_mealie(client: MealieApiClient) -> list[Any]:
    recipes = client.get_all_recipes()
    kptncook_recipes = []
    # One detail fetch per recipe to read extras; show progress since this is
    # the slow part of a sync on a large library.
    for recipe in track(
        recipes,
        description="Scanning Mealie recipes",
        total=len(recipes),
    ):
        detail = client.get_via_slug(recipe.slug)
        if detail.extras.get("source") == "kptncook":
            kptncook_recipes.append(detail)
    return kptncook_recipes


def get_kptncook_recipes_from_repository():
    return load_kptncook_recipes_from_repository().recipes


def get_recipe_from_repository_by_oid(oid: str):
    return load_recipe_from_repository_by_oid(oid=oid).recipes


def _resolve_recipe_summaries(
    client: KptnCookClient, items: Sequence[object], *, action: str
) -> list[RecipeInDb]:
    if not items:
        return []
    try:
        return client.resolve_recipe_summaries(items)
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action=action)
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc


def list_dailies(
    *,
    recipe_filter: str | None = None,
    zone: str | None = None,
    is_subscribed: bool | None = None,
) -> list[RecipeInDb]:
    try:
        return KptnCookClient().list_dailies(
            recipe_filter=recipe_filter,
            zone=zone,
            is_subscribed=is_subscribed,
        )
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="fetching dailies")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc


def _require_access_token() -> None:
    settings = get_settings()
    if settings.kptncook_access_token is None:
        raise UserFacingError(
            f"Please set KPTNCOOK_ACCESS_TOKEN in your environment or {ENV_PATH}"
        )


def sync_with_mealie_result() -> SyncWithMealieResult:
    client = get_mealie_client()
    kptncook_recipes_from_mealie = get_kptncook_recipes_from_mealie(client)
    repository_result = load_kptncook_recipes_from_repository()
    kptncook_recipes_from_repository = [
        kptncook_to_mealie(r) for r in repository_result.recipes
    ]
    ids_in_mealie = {r.extras["kptncook_id"] for r in kptncook_recipes_from_mealie}
    ids_from_api = {r.extras["kptncook_id"] for r in kptncook_recipes_from_repository}
    ids_to_add = ids_from_api - ids_in_mealie
    # Also guard by name: Mealie appends "(1)" on any name collision, and a
    # recipe's kptncook_id can change between fetches (e.g. dailies), so id-only
    # dedup would recreate the same recipe under a numbered name.
    names_in_mealie = {
        _normalize_recipe_name(recipe.name or "")
        for recipe in client.get_all_recipes()
        if recipe.name
    }
    recipes_to_add = [
        recipe
        for recipe in kptncook_recipes_from_repository
        if recipe.extras.get("kptncook_id") in ids_to_add
        and _normalize_recipe_name(recipe.name or "") not in names_in_mealie
    ]
    created_slugs: list[str] = []
    for recipe in track(
        recipes_to_add,
        description="Syncing to Mealie",
        total=len(recipes_to_add),
    ):
        try:
            created = client.create_recipe(recipe)
            created_slugs.append(created.slug)
        except httpx.HTTPStatusError as exc:
            detail_message = extract_mealie_detail_message(exc.response)
            if detail_message == "Recipe already exists":
                continue
            logger.warning(
                "Failed to create recipe %s in Mealie (%s): %s",
                recipe.name,
                exc.response.status_code,
                detail_message or exc,
            )
    return SyncWithMealieResult(
        created_count=len(created_slugs),
        skipped_count=len(kptncook_recipes_from_repository) - len(recipes_to_add),
        repository_count=len(kptncook_recipes_from_repository),
        invalid_repository_entries=repository_result.invalid_entries,
    )


def sync_with_mealie() -> int:
    return sync_with_mealie_result().created_count


def sync_dailies_with_mealie_result() -> DailiesSyncResult:
    """Fetch today's recipes and add only the new ones to Mealie.

    Deduplicates by recipe name (not kptncook id), so re-running for the daily
    picks never creates "(1)" name collisions, and it skips the full-library
    scan that `sync-with-mealie` does.
    """
    client = get_mealie_client()
    names_in_mealie = {
        _normalize_recipe_name(recipe.name or "")
        for recipe in client.get_all_recipes()
        if recipe.name
    }
    todays = get_today_recipes()
    saved = _save_repository_entries(todays)

    mealie_recipes = []
    invalid = 0
    for entry in todays:
        try:
            kptncook_recipe = Recipe.model_validate(entry.data)
        except ValidationError:
            invalid += 1
            continue
        mealie_recipes.append(kptncook_to_mealie(kptncook_recipe))

    to_create = [
        mealie_recipe
        for mealie_recipe in mealie_recipes
        if _normalize_recipe_name(mealie_recipe.name or "") not in names_in_mealie
    ]
    created = 0
    for mealie_recipe in track(
        to_create, description="Adding dailies to Mealie", total=len(to_create)
    ):
        try:
            client.create_recipe(mealie_recipe)
            created += 1
            names_in_mealie.add(_normalize_recipe_name(mealie_recipe.name or ""))
        except httpx.HTTPStatusError as exc:
            detail_message = extract_mealie_detail_message(exc.response)
            if detail_message == "Recipe already exists":
                continue
            logger.warning(
                "Failed to create daily %s in Mealie (%s): %s",
                mealie_recipe.name,
                exc.response.status_code,
                detail_message or exc,
            )
    return DailiesSyncResult(
        saved_count=saved,
        created_count=created,
        skipped_count=len(mealie_recipes) - len(to_create),
        invalid_count=invalid,
    )


# Matches Mealie's auto-deduplicated names like "Foo (1)", "Foo (2)".
_NUMBERED_SUFFIX = re.compile(r"^(?P<base>.+?)\s*\((?P<n>\d+)\)$")


def _normalize_recipe_name(name: str) -> str:
    # Collapse whitespace runs and strip; Mealie sometimes stores stray trailing
    # or duplicated spaces that would otherwise break exact base-name matching.
    return " ".join(name.split())


def _strip_numbered_suffix(name: str) -> str:
    # "Foo (1)" -> "Foo"; leaves un-suffixed names unchanged.
    match = _NUMBERED_SUFFIX.match(name or "")
    return match.group("base") if match else (name or "")


def _find_numbered_duplicates(recipes: Sequence[Any]) -> list[MealieRecipeRef]:
    existing_names = {
        _normalize_recipe_name(recipe.name) for recipe in recipes if recipe.name
    }
    duplicates: list[MealieRecipeRef] = []
    for recipe in recipes:
        name = recipe.name or ""
        match = _NUMBERED_SUFFIX.match(name)
        if match is None:
            continue
        base = _normalize_recipe_name(match.group("base"))
        # Only a duplicate if the un-suffixed name also exists in Mealie.
        if base and base in existing_names:
            duplicates.append(MealieRecipeRef(name=name, slug=recipe.slug))
    duplicates.sort(key=lambda duplicate: duplicate.name)
    return duplicates


def find_mealie_duplicates() -> list[MealieRecipeRef]:
    client = get_mealie_client()
    try:
        recipes = client.get_all_recipes()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="listing Mealie recipes")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    return _find_numbered_duplicates(recipes)


def delete_mealie_duplicates(
    duplicates: Sequence[MealieRecipeRef],
) -> MealieDeleteResult:
    if not duplicates:
        return MealieDeleteResult(deleted=[], failed=[])
    client = get_mealie_client()
    deleted: list[MealieRecipeRef] = []
    failed: list[tuple[MealieRecipeRef, str]] = []
    for duplicate in track(
        duplicates,
        description="Deleting duplicates",
        total=len(duplicates),
    ):
        try:
            client.delete_via_slug(duplicate.slug)
            deleted.append(duplicate)
        except httpx.HTTPError as exc:
            failed.append((duplicate, str(exc)))
    return MealieDeleteResult(deleted=deleted, failed=failed)


def _is_empty_recipe(recipe_data: dict) -> bool:
    # Failed imports leave a bare recipe with no steps and no ingredients;
    # Mealie renders its "1 Cup Flour" / markdown-hint placeholders for these.
    # Read the raw payload because the Recipe model doesn't alias these fields.
    steps = recipe_data.get("recipeInstructions") or []
    ingredients = recipe_data.get("recipeIngredient") or []
    return not steps and not ingredients


def find_mealie_empty_recipes() -> list[MealieRecipeRef]:
    client = get_mealie_client()
    try:
        summaries = client.get_all_recipes()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="listing Mealie recipes")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    empty: list[MealieRecipeRef] = []
    # The list endpoint omits steps/ingredients, so each recipe needs a detail
    # fetch to tell empty imports apart from real recipes.
    for summary in track(
        summaries,
        description="Scanning Mealie recipes",
        total=len(summaries),
    ):
        try:
            detail = client.get_recipe_dict(summary.slug)
        except httpx.HTTPError:
            continue
        if _is_empty_recipe(detail):
            name = detail.get("name") or summary.name or summary.slug
            empty.append(MealieRecipeRef(name=name, slug=summary.slug))
    empty.sort(key=lambda ref: ref.name)
    return empty


# Mealie's default step, left in place when an import never populated the steps.
_DEFAULT_STEP_PREFIX = (
    "Recipe steps as well as other fields in the recipe page support markdown syntax."
)


def _is_broken_recipe(recipe_data: dict) -> bool:
    # A failed import either has no steps at all or only Mealie's default
    # placeholder step (even when ingredients were imported).
    steps = recipe_data.get("recipeInstructions") or []
    if not steps:
        return True
    if len(steps) == 1:
        text = (steps[0].get("text") or "").strip()
        return text.startswith(_DEFAULT_STEP_PREFIX)
    return False


def find_mealie_broken_recipes() -> list[MealieRecipeRef]:
    client = get_mealie_client()
    try:
        summaries = client.get_all_recipes()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="listing Mealie recipes")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    broken: list[MealieRecipeRef] = []
    for summary in track(
        summaries,
        description="Scanning Mealie recipes",
        total=len(summaries),
    ):
        try:
            detail = client.get_recipe_dict(summary.slug)
        except httpx.HTTPError:
            continue
        if _is_broken_recipe(detail):
            name = detail.get("name") or summary.name or summary.slug
            broken.append(MealieRecipeRef(name=name, slug=summary.slug))
    broken.sort(key=lambda ref: ref.name)
    return broken


def _repository_recipes_by_name() -> dict[str, Any]:
    repository_result = load_kptncook_recipes_from_repository()
    by_name: dict[str, Any] = {}
    for recipe in repository_result.recipes:
        mealie_recipe = kptncook_to_mealie(recipe)
        key = _normalize_recipe_name(mealie_recipe.name or "")
        # Keep the first match; later duplicates in the repo don't matter here.
        by_name.setdefault(key, mealie_recipe)
    return by_name


def repair_mealie_recipes(
    broken: Sequence[MealieRecipeRef],
) -> MealieRepairResult:
    if not broken:
        return MealieRepairResult(repaired=[], unmatched=[], failed=[])
    repo_by_name = _repository_recipes_by_name()
    client = get_mealie_client()
    existing_names = {
        _normalize_recipe_name(recipe.name or "")
        for recipe in client.get_all_recipes()
        if recipe.name
    }
    repaired: list[MealieRecipeRef] = []
    unmatched: list[MealieRecipeRef] = []
    failed: list[tuple[MealieRecipeRef, str]] = []

    def _recreate(source: Any, base_name: str) -> tuple[bool, str | None]:
        try:
            client.create_recipe(source.model_copy(deep=True))
        except httpx.HTTPStatusError as exc:
            # Mealie rejects duplicate names outright, so treat that as "the
            # clean recipe already exists" rather than a failure.
            if extract_mealie_detail_message(exc.response) == "Recipe already exists":
                existing_names.add(base_name)
                return True, None
            return False, f"recreate failed: {exc}"
        except httpx.HTTPError as exc:
            return False, f"recreate failed: {exc}"
        existing_names.add(base_name)
        return True, None

    def _delete(ref: MealieRecipeRef) -> tuple[bool, str | None]:
        try:
            client.delete_via_slug(ref.slug)
        except httpx.HTTPError as exc:
            return False, f"delete failed: {exc}"
        existing_names.discard(_normalize_recipe_name(ref.name))
        return True, None

    for ref in track(
        broken,
        description="Repairing recipes",
        total=len(broken),
    ):
        # Failed imports are often named "<name> (N)"; match the un-suffixed name.
        base_name = _normalize_recipe_name(_strip_numbered_suffix(ref.name))
        source = repo_by_name.get(base_name)
        if source is None:
            unmatched.append(ref)
            continue
        is_suffixed = _normalize_recipe_name(ref.name) != base_name
        if is_suffixed:
            if base_name in existing_names:
                # A clean copy already exists; the broken one is just a duplicate.
                ok, err = _delete(ref)
            else:
                # Recreate the clean recipe first (the name is free), then remove
                # the broken copy, so a failure never loses the only version.
                ok, err = _recreate(source, base_name)
                if ok:
                    ok, err = _delete(ref)
        else:
            # A clean-named broken recipe occupies the name; delete then recreate.
            ok, err = _delete(ref)
            if ok:
                ok, err = _recreate(source, base_name)
        if ok:
            repaired.append(ref)
        else:
            failed.append((ref, err or "failed"))
    return MealieRepairResult(repaired=repaired, unmatched=unmatched, failed=failed)


def _tag_id_map(client: MealieApiClient) -> dict[str, str]:
    # Case-insensitive lookup by tag name (and slug as a fallback).
    mapping: dict[str, str] = {}
    for tag in client.get_all_tags():
        tag_id = tag.get("id")
        if not tag_id:
            continue
        name = tag.get("name")
        slug = tag.get("slug")
        if name:
            mapping[name.casefold()] = tag_id
        if slug:
            mapping.setdefault(slug.casefold(), tag_id)
    return mapping


def _cookbook_title(tag: str) -> str:
    return tag.replace("_", " ").title()


def create_mealie_cookbooks(
    *,
    category_tags: Sequence[str],
    require_tags: Sequence[str],
    public: bool = False,
    dry_run: bool = False,
) -> CookbookSyncResult:
    client = get_mealie_client()
    try:
        tag_ids = _tag_id_map(client)
        existing = {cb["name"]: cb for cb in client.get_cookbooks()}
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(
                exc.response, action="reading Mealie tags/cookbooks"
            )
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc

    missing_required = [t for t in require_tags if t.casefold() not in tag_ids]
    if missing_required:
        raise UserFacingError(
            "Required tags not found in Mealie: " + ", ".join(missing_required)
        )
    require_ids = [tag_ids[t.casefold()] for t in require_tags]

    created: list[str] = []
    updated: list[str] = []
    missing_tags: list[str] = []
    for tag in category_tags:
        key = tag.casefold()
        if key not in tag_ids:
            missing_tags.append(tag)
            continue
        ids = [tag_ids[key], *require_ids]
        query = "tags.id CONTAINS ALL [" + ",".join(f'"{i}"' for i in ids) + "]"
        title = _cookbook_title(tag)
        parts = [tag, *require_tags]
        payload = {
            "name": title,
            "description": "kptncook: " + " + ".join(parts),
            "public": public,
            "queryFilterString": query,
        }
        if dry_run:
            (updated if title in existing else created).append(title)
            continue
        if title in existing:
            current = existing[title]
            client.update_cookbook(current["id"], {**current, **payload})
            updated.append(title)
        else:
            client.create_cookbook(payload)
            created.append(title)
    return CookbookSyncResult(
        created=created, updated=updated, missing_tags=missing_tags
    )


DEFAULT_CATEGORY_RULES: tuple[CategoryRule, ...] = (
    CategoryRule(("diet_vegetarian", "main_dish", "peanut_free"), "main_vegetarian"),
    CategoryRule(("diet_high_protein", "main_dish", "peanut_free"), "main_high_protein"),
    CategoryRule(("cooking_time_under_20", "main_dish", "peanut_free"), "main_under_20"),
)

# kptncook encodes equipment as active tags; map the real ones to Mealie tools.
DEFAULT_TOOL_MAP: dict[str, str] = {
    "one_pot": "One Pot",
    "casserole_dish": "Casserole Dish",
    "grilled": "Grill",
    "airfryer": "Air Fryer",
    "muffin_tin": "Muffin Tin",
    "waffle_iron": "Waffle Iron",
}

KPTNCOOK_CATEGORY = "kptncook"

# (slug, source, tag names, existing tool names)
_RecipeRecord = tuple[str, str | None, set[str], set[str]]


def _plan_categorization(
    records: Sequence[_RecipeRecord],
    rules: Sequence[CategoryRule],
    tool_map: dict[str, str],
    add_tools: bool,
) -> tuple[list[str], dict[str, list[str]], dict[str, set[str]]]:
    kptncook_slugs = [slug for slug, source, _, _ in records if source == "kptncook"]
    rule_slugs: dict[str, list[str]] = {}
    for rule in rules:
        required = set(rule.require_tags)
        rule_slugs[rule.category] = [
            slug for slug, _, tags, _ in records if required <= tags
        ]
    # slug -> full merged set of tool names to store (existing + newly derived)
    tool_assignments: dict[str, set[str]] = {}
    if add_tools:
        for slug, _, tags, existing_tools in records:
            wanted = {tool_map[t] for t in tags if t in tool_map}
            if wanted - existing_tools:
                tool_assignments[slug] = existing_tools | wanted
    return kptncook_slugs, rule_slugs, tool_assignments


def _bulk_categorize(
    client: MealieApiClient, slugs: Sequence[str], category: dict
) -> None:
    payload_category = [
        {"id": category["id"], "name": category["name"], "slug": category["slug"]}
    ]
    for start in range(0, len(slugs), 200):
        client.bulk_categorize(list(slugs[start : start + 200]), payload_category)


def categorize_mealie_recipes(
    *,
    rules: Sequence[CategoryRule] = DEFAULT_CATEGORY_RULES,
    tool_map: dict[str, str] | None = None,
    add_tools: bool = True,
    fix_cookbooks: bool = True,
    dry_run: bool = False,
) -> CategorizeResult:
    tool_map = DEFAULT_TOOL_MAP if tool_map is None else tool_map
    client = get_mealie_client()
    try:
        summaries = client.get_all_recipes()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="listing Mealie recipes")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc

    # Single pass: fetch each recipe's tags, source and existing tools.
    records: list[_RecipeRecord] = []
    for summary in track(
        summaries, description="Scanning Mealie recipes", total=len(summaries)
    ):
        try:
            detail = client.get_recipe_dict(summary.slug)
        except httpx.HTTPError:
            continue
        tags = {t.get("name") for t in (detail.get("tags") or []) if t.get("name")}
        recipe_tools = {
            t.get("name") for t in (detail.get("tools") or []) if t.get("name")
        }
        source = (detail.get("extras") or {}).get("source")
        records.append((summary.slug, source, tags, recipe_tools))  # type: ignore[arg-type]

    kptncook_slugs, rule_slugs, tool_assignments = _plan_categorization(
        records, rules, tool_map, add_tools
    )
    rule_counts = {rule.category: len(rule_slugs[rule.category]) for rule in rules}
    tool_counts: dict[str, int] = {}
    for names in tool_assignments.values():
        for name in names:
            if name in tool_map.values():
                tool_counts[name] = tool_counts.get(name, 0) + 1

    if dry_run:
        return CategorizeResult(
            scanned=len(records),
            kptncook_count=len(kptncook_slugs),
            rule_counts=rule_counts,
            tool_counts=tool_counts,
            cookbooks_updated=[],
        )

    categories = {c["name"]: c for c in client.get_all_categories()}

    def ensure_category(name: str) -> dict:
        if name not in categories:
            categories[name] = client.create_category(name)
        return categories[name]

    if kptncook_slugs:
        _bulk_categorize(client, kptncook_slugs, ensure_category(KPTNCOOK_CATEGORY))
    for rule in rules:
        slugs = rule_slugs[rule.category]
        if slugs:
            _bulk_categorize(client, slugs, ensure_category(rule.category))

    if add_tools and tool_assignments:
        tools = {t["name"]: t for t in client.get_all_tools()}

        def ensure_tool(name: str) -> dict:
            if name not in tools:
                tools[name] = client.create_tool(name)
            return tools[name]

        for slug, tool_names in track(
            tool_assignments.items(),
            description="Setting recipe tools",
            total=len(tool_assignments),
        ):
            payload_tools = [
                {"id": (t := ensure_tool(n))["id"], "name": t["name"], "slug": t["slug"]}
                for n in sorted(tool_names)
            ]
            try:
                client.patch_recipe(slug, {"tools": payload_tools})
            except httpx.HTTPError:
                continue

    cookbooks_updated: list[str] = []
    if fix_cookbooks:
        # Repoint each rule's cookbook to filter by its single category, which
        # (unlike multi-tag CONTAINS ALL) paginates correctly in Mealie.
        existing_cookbooks = {cb["name"]: cb for cb in client.get_cookbooks()}
        for rule in rules:
            title = _cookbook_title(rule.require_tags[0])
            cookbook = existing_cookbooks.get(title)
            category = categories.get(rule.category)
            if cookbook is None or category is None:
                continue
            query = f'recipe_category.id IN ["{category["id"]}"]'
            client.update_cookbook(
                cookbook["id"], {**cookbook, "queryFilterString": query}
            )
            cookbooks_updated.append(title)

    return CategorizeResult(
        scanned=len(records),
        kptncook_count=len(kptncook_slugs),
        rule_counts=rule_counts,
        tool_counts=tool_counts,
        cookbooks_updated=cookbooks_updated,
    )



def backup_kptncook_favorites() -> FavoritesBackupResult:
    _require_access_token()
    client = KptnCookClient()
    try:
        favorites = client.list_favorites()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(
                exc.response,
                action="fetching favorites",
                unavailable_on_redirect=True,
            )
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    except ValueError as exc:
        raise UserFacingError(str(exc)) from exc

    identifiers = _collect_recipe_identifiers(favorites)
    if not identifiers:
        raise UserFacingError("Could not find any favorites")

    recipes = _resolve_recipe_summaries(client, identifiers, action="resolving recipes")
    if len(recipes) == 0:
        raise UserFacingError("Could not find any favorites")

    saved_count = _save_repository_entries(recipes)
    return FavoritesBackupResult(
        favorite_count=len(favorites),
        saved_count=saved_count,
    )


def get_kptncook_access_token() -> str:
    settings = get_settings()
    username, password = get_credentials(
        username_command=settings.kptncook_username_command,
        password_command=settings.kptncook_password_command,
    )
    if not username or not password:
        raise UserFacingError("Failed to get credentials")

    client = KptnCookClient()
    try:
        return client.get_access_token(username, password)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            message = (
                "Login failed (HTTP 401). Check your email/password and make sure "
                "KPTNCOOK_API_KEY is set to your real API key (not a placeholder)."
            )
        else:
            message = format_http_status_error(
                exc.response, action="getting access token"
            )
        raise UserFacingError(message) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc


def get_discovery_screen() -> DiscoveryScreenData:
    try:
        payload = KptnCookClient().get_discovery_screen()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="fetching discovery screen")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    return parse_discovery_screen(payload)


def get_discovery_list_recipes(
    *, list_type: str, list_id: str | None
) -> list[RecipeInDb]:
    client = KptnCookClient()
    try:
        items = client.get_discovery_list(list_type=list_type, list_id=list_id)
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="fetching discovery list")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    return _resolve_recipe_summaries(client, items, action="resolving recipes")


def list_popular_ingredients() -> list[dict[str, object]]:
    _require_access_token()
    client = KptnCookClient()
    try:
        return client.list_popular_ingredients()
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(
                exc.response, action="fetching popular ingredients"
            )
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc


def get_recipes_with_ingredients(ingredient_ids: list[str]) -> list[RecipeInDb]:
    _require_access_token()
    client = KptnCookClient()
    try:
        items = client.get_recipes_with_ingredients(ingredient_ids=ingredient_ids)
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(
                exc.response,
                action="fetching recipes with ingredients",
            )
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    return _resolve_recipe_summaries(client, items, action="resolving recipes")


def get_onboarding_recipes(tags: list[str]) -> list[RecipeInDb]:
    client = KptnCookClient()
    try:
        items = client.get_onboarding_recipes(tags=tags)
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="fetching onboarding recipes")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc
    return _resolve_recipe_summaries(client, items, action="resolving recipes")


def delete_recipes_by_selection(
    *,
    indices: list[int],
    oids: list[str],
) -> DeleteSelectionResult:
    repository_result = load_kptncook_recipes_from_repository()
    recipes = repository_result.recipes
    index_ids: list[str] = []
    invalid_indices: list[int] = []
    for index in indices:
        if index < 0 or index >= len(recipes):
            invalid_indices.append(index)
            continue
        index_ids.append(recipes[index].id.oid)

    requested_ids: list[str] = []
    for oid in index_ids + oids:
        if oid not in requested_ids:
            requested_ids.append(oid)

    existing_ids = {str(key) for key in _repository_id_map().keys()}
    missing_ids = [oid for oid in requested_ids if str(oid) not in existing_ids]
    to_delete_ids = [oid for oid in requested_ids if str(oid) in existing_ids]
    return DeleteSelectionResult(
        recipes=recipes,
        invalid_indices=invalid_indices,
        missing_ids=missing_ids,
        to_delete_ids=to_delete_ids,
        invalid_repository_entries=repository_result.invalid_entries,
    )


def delete_repository_recipes(ids: list[str]) -> tuple[list[str], list[str]]:
    return _delete_repository_ids(ids)


def search_recipe_by_id(id_: str) -> SearchResult:
    resolved_id = id_
    if resolved_id.startswith("https://share.kptncook.com/"):
        try:
            response = httpx.get(resolved_id, timeout=SHARE_URL_TIMEOUT)
        except httpx.HTTPError as exc:
            raise UserFacingError(
                f"Request failed while resolving share URL: {exc}"
            ) from exc
        if response.status_code not in (301, 302):
            raise UserFacingError(
                f"Could not get redirect location (HTTP {response.status_code})."
            )
        location = response.headers.get("location")
        if not location:
            raise UserFacingError("Share URL did not include a redirect location.")
        resolved_id = location

    parsed = parse_id(resolved_id)
    if parsed is None:
        raise UserFacingError("Could not parse id")

    id_type, id_value = parsed
    try:
        recipes = KptnCookClient().get_by_ids([(id_type, id_value)])
    except httpx.HTTPStatusError as exc:
        raise UserFacingError(
            format_http_status_error(exc.response, action="fetching recipe")
        ) from exc
    except httpx.HTTPError as exc:
        raise UserFacingError(format_request_error(exc)) from exc

    if len(recipes) == 0:
        raise UserFacingError("Could not find recipe")

    recipe = recipes[0]
    _save_repository_entries([recipe])
    return SearchResult(id_type=id_type, id_value=id_value, recipe=recipe)


def get_recipe_by_id(id_: str):
    found_recipes = load_recipe_from_repository_by_id(id_).recipes
    if len(found_recipes) == 0:
        raise UserFacingError("Recipe not found.")
    if len(found_recipes) > 1:
        raise UserFacingError("More than one recipe found with that ID.")
    return found_recipes


def export_recipes_to_paprika_result(recipe_id: str | None) -> PaprikaExportResult:
    repository_result = (
        load_recipe_from_repository_by_id(recipe_id)
        if recipe_id
        else load_kptncook_recipes_from_repository()
    )
    recipes = repository_result.recipes
    if recipe_id:
        if len(recipes) == 0:
            raise UserFacingError("Recipe not found.")
        if len(recipes) > 1:
            raise UserFacingError("More than one recipe found with that ID.")
    return PaprikaExportResult(
        filename=PaprikaExporter().export(recipes=recipes),
        invalid_repository_entries=repository_result.invalid_entries,
    )


def export_recipes_to_paprika(recipe_id: str | None) -> str:
    return export_recipes_to_paprika_result(recipe_id).filename


def export_recipes_to_tandoor_result(recipe_id: str | None) -> TandoorExportResult:
    repository_result = (
        load_recipe_from_repository_by_id(recipe_id)
        if recipe_id
        else load_kptncook_recipes_from_repository()
    )
    recipes = repository_result.recipes
    if recipe_id:
        if len(recipes) == 0:
            raise UserFacingError("Recipe not found.")
        if len(recipes) > 1:
            raise UserFacingError("More than one recipe found with that ID.")
    return TandoorExportResult(
        filenames=TandoorExporter().export(recipes=recipes),
        invalid_repository_entries=repository_result.invalid_entries,
    )


def export_recipes_to_tandoor(recipe_id: str | None) -> list[str]:
    return export_recipes_to_tandoor_result(recipe_id).filenames


def export_recipes_to_markdown_result(recipe_id: str | None) -> MarkdownExportResult:
    repository_result = (
        load_recipe_from_repository_by_id(recipe_id)
        if recipe_id
        else load_kptncook_recipes_from_repository()
    )
    recipes = repository_result.recipes
    if recipe_id:
        if len(recipes) == 0:
            raise UserFacingError("Recipe not found.")
        if len(recipes) > 1:
            raise UserFacingError("More than one recipe found with that ID.")
    return MarkdownExportResult(
        filenames=[str(path) for path in MarkdownExporter().export(recipes=recipes)],
        invalid_repository_entries=repository_result.invalid_entries,
    )


def export_recipes_to_markdown(recipe_id: str | None) -> list[str]:
    return export_recipes_to_markdown_result(recipe_id).filenames
