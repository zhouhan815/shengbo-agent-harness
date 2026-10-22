"""Role-scoped Skill export and native progressive-disclosure discovery."""
from pathlib import Path

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.skills import SkillsMiddleware

from shengbo_contracts.common import ContractError
import shengbo_prelive.runtime as runtime


def write_skill(project, folder="prelive", *, role="main", name="prelive-main", body="Base instructions"):
    root = project / "skills" / role / folder
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: Use for this isolated test\n---\n{body}\n", encoding="utf-8")
    return root


@pytest.fixture
def skill_tree(tmp_path, monkeypatch):
    # Keep the production API fixed to the installed project, not caller paths.
    # The isolated test replaces its module location rather than opening a
    # runtime escape hatch for arbitrary host Skill directories.
    module = tmp_path / "src" / "shengbo_prelive" / "runtime.py"
    module.parent.mkdir(parents=True)
    module.write_text("# isolated module location", encoding="utf-8")
    monkeypatch.setattr(runtime, "__file__", str(module))
    write_skill(tmp_path)
    return tmp_path


def initial_files():
    return runtime.RuntimeEngine._initial_files(None, "main")


def test_multiple_role_skills_preserve_base_path_and_native_discovery(skill_tree, monkeypatch):
    specialist = write_skill(skill_tree, "core-selling-points", name="core-selling-points", body="Private body marker")
    (specialist / "references").mkdir()
    (specialist / "references" / "examples.md").write_text("On-demand reference marker", encoding="utf-8")
    write_skill(skill_tree, role="audience_scenes", name="prelive-audience-scenes")
    files = initial_files()
    assert list(files)[0] == "/skills/main/prelive-main/SKILL.md"
    assert set(files) == {
        "/skills/main/prelive-main/SKILL.md",
        "/skills/main/core-selling-points/SKILL.md",
        "/skills/main/core-selling-points/references/examples.md",
    }
    backend = StateBackend()
    monkeypatch.setattr(backend, "_read_files", lambda: files)
    middleware = SkillsMiddleware(backend=backend, sources=["/skills/main/"])
    metadata = middleware.before_agent({"files": files}, None, {})
    assert not metadata["skills_load_errors"]
    assert {item["name"] for item in metadata["skills_metadata"]} == {"prelive-main", "core-selling-points"}
    discovery_text = middleware._format_skills_list(metadata["skills_metadata"])
    assert "Private body marker" not in discovery_text
    assert "On-demand reference marker" not in discovery_text
    assert "core-selling-points/SKILL.md" in discovery_text
    assert file_data_to_string(files["/skills/main/core-selling-points/references/examples.md"]) == "On-demand reference marker"


def test_only_controlled_reference_text_is_exported(skill_tree):
    root = skill_tree / "skills" / "main" / "prelive"
    for relative in ["references/sub/a.md", "references/data.json", "references/info.txt", "references/helper.py",
                     "references/.env", "references/token.key", "scripts/program.py", "assets/image.png"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture-only", encoding="utf-8")
    (skill_tree / "skills" / "main" / "not-a-skill").mkdir()
    (skill_tree / "skills" / "main" / "credentials.txt").write_text("not imported", encoding="utf-8")
    files = initial_files()
    assert set(files) == {
        "/skills/main/prelive-main/SKILL.md",
        "/skills/main/prelive-main/references/sub/a.md",
        "/skills/main/prelive-main/references/data.json",
        "/skills/main/prelive-main/references/info.txt",
    }


@pytest.mark.parametrize("name", ["../escape", "bad/name", "bad\\name", "Upper", "bad--name", "-bad", "bad-", "x" * 65])
def test_invalid_metadata_cannot_create_virtual_paths(skill_tree, name):
    write_skill(skill_tree, "invalid", name=name)
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_METADATA_INVALID"


@pytest.mark.parametrize("body", ["# no frontmatter", "---\nname: valid\n---\nmissing description",
                                  "---\nname: [invalid\ndescription: test\n---\n"])
def test_invalid_frontmatter_fails_clearly(skill_tree, body):
    root = write_skill(skill_tree, "invalid", name="invalid")
    (root / "SKILL.md").write_text(body, encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_METADATA_INVALID"


def test_duplicate_names_cannot_silently_override_base(skill_tree):
    write_skill(skill_tree, "shadow", name="prelive-main")
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_NAME_CONFLICT"


@pytest.mark.parametrize("relative", ["SKILL.md", "references/too-large.md"])
def test_oversized_file_rejected_before_export(skill_tree, relative):
    path = skill_tree / "skills" / "main" / "prelive" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_REFERENCE_SIZE_INVALID"


def test_per_skill_reference_budget_cannot_be_exceeded(skill_tree):
    root = skill_tree / "skills" / "main" / "prelive" / "references"
    root.mkdir()
    for number in range(4):
        (root / f"{number}.txt").write_bytes(b"x" * (256 * 1024))
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_REFERENCE_SIZE_INVALID"


def test_combined_role_budget_is_bounded(skill_tree):
    for number in range(6):
        root = write_skill(skill_tree, f"extra-{number}", name=f"extra-{number}") / "references"
        root.mkdir()
        for item in range(3):
            (root / f"{item}.txt").write_bytes(b"x" * (256 * 1024))
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_REFERENCE_SIZE_INVALID"


def test_non_utf8_reference_is_not_copied_as_text(skill_tree):
    reference = skill_tree / "skills" / "main" / "prelive" / "references" / "binary.txt"
    reference.parent.mkdir()
    reference.write_bytes(b"\xff\xfe")
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_CONTENT_INVALID"


@pytest.mark.parametrize("role", ["../main", "/main", "undeclared"])
def test_roles_are_not_arbitrary_directory_inputs(role):
    with pytest.raises(ContractError) as caught:
        runtime.RuntimeEngine._initial_files(None, role)
    assert caught.value.code == "SKILL_ROLE_INVALID"


def make_link(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Host does not permit symlink creation: {type(exc).__name__}")


def test_linked_skill_and_reference_files_are_never_read(skill_tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("do-not-export", encoding="utf-8")
    external_skill = write_skill(outside, name="outside-skill")
    make_link(skill_tree / "skills" / "main" / "linked-skill", external_skill, directory=True)
    root = skill_tree / "skills" / "main" / "prelive" / "references"
    root.mkdir()
    make_link(root / "linked.txt", outside / "private.txt")
    make_link(root / "linked-dir", outside, directory=True)
    files = initial_files()
    assert set(files) == {"/skills/main/prelive-main/SKILL.md"}


def test_linked_role_directory_rejected(skill_tree):
    original = skill_tree / "skills" / "main"
    relocated = skill_tree / "role-outside"
    original.rename(relocated)
    make_link(original, relocated, directory=True)
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_SOURCE_INVALID"


def test_reparse_directory_never_exposes_descendant_text(skill_tree, monkeypatch):
    # Symlink creation requires a host privilege on Windows; exercise the
    # guarded ancestor branch independently even when the OS denies creation.
    root = skill_tree / "skills" / "main" / "prelive" / "references" / "linked-dir"
    root.mkdir(parents=True)
    (root / "private.txt").write_text("do-not-export", encoding="utf-8")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root or original(path))
    files = initial_files()
    assert set(files) == {"/skills/main/prelive-main/SKILL.md"}


def test_missing_required_base_skill_rejected(skill_tree):
    (skill_tree / "skills" / "main" / "prelive" / "SKILL.md").unlink()
    write_skill(skill_tree, "core-selling-points", name="core-selling-points")
    with pytest.raises(ContractError) as caught:
        initial_files()
    assert caught.value.code == "SKILL_SOURCE_INVALID"


def test_actual_project_skills_include_marketing_skill_with_unchanged_base():
    files = initial_files()
    assert "/skills/main/prelive-orchestration/SKILL.md" in files
    assert "/skills/main/core-selling-points/SKILL.md" in files
    assert "/skills/main/core-selling-points/references/value-writing-examples.md" in files
    assert all(path.startswith("/skills/main/") for path in files)


@pytest.mark.parametrize("role", ["product_facts", "session_config", "audience_scenes"])
def test_existing_worker_skill_paths_remain_role_scoped(role):
    files = runtime.RuntimeEngine._initial_files(None, role)
    assert f"/skills/{role}/prelive-{role.replace('_', '-')}/SKILL.md" in files
    assert all(path.startswith(f"/skills/{role}/") for path in files)
