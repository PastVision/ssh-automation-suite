"""Variable rendering: correctness and injection resistance.

Every injection case is executed by a real shell, not merely compared as a string.
A renderer can produce text that *looks* quoted and still be wrong (an unbalanced
quote, an unescaped ``$``), so the assertion is that the shell sees the value as
data: one argument, or one line of output, never a new command.
"""

from __future__ import annotations

import subprocess

import pytest

from app.services import variables

# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["hostname", "os_version", "disk_usage", "_private", "A1", "a" * 64],
)
def test_valid_names_are_accepted(name: str) -> None:
    assert variables.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "1leading",
        "has space",
        "has-dash",
        "has.dot",
        "a" * 65,
        "shell$var",
        "brace{name}",
        "semi;colon",
        "back`tick`",
        "quote'name",
        "new\nline",
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    with pytest.raises(variables.RenderError):
        variables.validate_name(name)


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(variables.RenderError, match="more than once"):
        variables.validate_unique(["hostname", "hostname"])


def test_variable_count_limit() -> None:
    variables.validate_unique([f"v{i}" for i in range(variables.MAX_VARIABLES)])
    with pytest.raises(variables.RenderError, match="at most"):
        variables.validate_unique([f"v{i}" for i in range(variables.MAX_VARIABLES + 1)])


# --------------------------------------------------------------------------- #
# Substitution basics
# --------------------------------------------------------------------------- #


def test_simple_substitution() -> None:
    result = variables.render("echo {{name}}", {"name": "web-01"})
    assert result.text == "echo 'web-01'"
    assert result.substituted == ("name",)


def test_whitespace_inside_braces_is_tolerated() -> None:
    assert variables.render("echo {{ name }}", {"name": "x"}).text == "echo 'x'"
    assert variables.render("echo {{  name  }}", {"name": "x"}).text == "echo 'x'"


def test_multiple_occurrences_are_all_substituted() -> None:
    result = variables.render("echo {{a}} {{a}} {{b}}", {"a": "1", "b": "2"})
    assert result.text == "echo '1' '1' '2'"
    assert result.substituted == ("a", "b")


def test_unreferenced_variables_are_not_in_substituted() -> None:
    result = variables.render("echo {{a}}", {"a": "1", "b": "2"})
    assert result.substituted == ("a",)


# --------------------------------------------------------------------------- #
# Undefined, malformed, recursive
# --------------------------------------------------------------------------- #


def test_undefined_variable_is_rejected() -> None:
    with pytest.raises(variables.RenderError, match="not defined"):
        variables.render("echo {{missing}}", {"name": "x"})


def test_undefined_variable_message_lists_defined_names() -> None:
    with pytest.raises(variables.RenderError) as excinfo:
        variables.render("{{missing}}", {"alpha": "1", "beta": "2"})
    assert "alpha" in str(excinfo.value) and "beta" in str(excinfo.value)


@pytest.mark.parametrize(
    "script",
    [
        "echo {{na me}}",
        "echo {{name}",
        "echo {{{name}}}",
        "echo {{name.attr}}",
        "echo {{name[0]}}",
        "echo {{func()}}",
        "echo {{1abc}}",
    ],
)
def test_malformed_references_are_rejected(script: str) -> None:
    with pytest.raises(variables.RenderError):
        variables.render(script, {"name": "x"})


def test_no_recursive_expansion() -> None:
    result = variables.render("echo {{a}}", {"a": "{{b}}", "b": "sneaky"})
    # The inserted text is never re-scanned, so it stays literal.
    assert result.text == "echo '{{b}}'"
    assert "sneaky" not in result.text


def test_python_style_formatting_is_not_interpreted() -> None:
    result = variables.render("echo {name}", {"name": "x"})
    assert result.text == "echo {name}"


# --------------------------------------------------------------------------- #
# Shell variables are preserved
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "script",
    [
        "echo $HOME",
        "echo ${PATH}",
        "echo $USER",
        'echo "$HOME/x"',
        "echo '${PATH}'",
        "echo $((1+2))",
    ],
)
def test_shell_variables_are_untouched(script: str) -> None:
    assert variables.render(script, {"name": "x"}).text == script


def test_shell_variable_next_to_a_reference() -> None:
    # `$HOME` survives untouched and sits immediately next to the substituted
    # value, which is escaped for its own double-quoted context.
    result = variables.render('echo "$HOME/{{dir}}"', {"dir": "app"})
    assert result.text == 'echo "$HOME/app"'



# --------------------------------------------------------------------------- #
# Quoting contexts, verified by a real shell
# --------------------------------------------------------------------------- #


def _bash_output(script: str) -> str:
    """Run ``script`` in a real Linux bash and return stdout.

    The host's ``bash`` is a Windows shim that mangles quoting, so the shell used
    is the Linux bash inside the Docker test lab, fed on stdin exactly as the
    execution engine feeds a template. That makes these assertions about the shell
    the product actually targets. Skips when the lab is down.
    """
    completed = subprocess.run(
        ["docker", "exec", "-i", "ssh-lab-ssh-host-01", "bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _require_bash() -> None:
    probe = subprocess.run(
        ["docker", "exec", "ssh-lab-ssh-host-01", "bash", "-c", "echo ok"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("Linux bash unavailable: start the Docker test lab")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("has space", "has space"),
        ("has'single", "has'single"),
        ('has"double', 'has"double'),
        ("has$dollar", "has$dollar"),
        ("has`tick`", "has`tick`"),
        ("has\\backslash", "has\\backslash"),
        ("-leading-dash", "-leading-dash"),
        ("semi;colon", "semi;colon"),
        ("new\nline", "new\nline"),
        ("", ""),
    ],
)
def test_bare_word_values_are_literal_in_shell(value: str, expected: str) -> None:
    _require_bash()
    rendered = variables.render("printf '%s' {{v}}", {"v": value}).text
    assert _bash_output(rendered) == expected


@pytest.mark.parametrize(
    "value",
    [
        "has'single",
        'has"double',
        "has$dollar",
        "has`tick`",
        "has\\backslash",
        "$(id)",
        "; rm -rf /tmp/x",
        "$(touch /tmp/pwned)",
    ],
)
def test_double_quoted_values_cannot_expand(value: str) -> None:
    _require_bash()
    rendered = variables.render('printf "%s" "{{v}}"', {"v": value}).text
    assert _bash_output(rendered) == value


@pytest.mark.parametrize(
    "value",
    ["has'single", 'has"double', "has$dollar", "$(id)", "`id`", "a'b"],
)
def test_single_quoted_values_cannot_break_out(value: str) -> None:
    _require_bash()
    rendered = variables.render("printf '%s' '{{v}}'", {"v": value}).text
    assert _bash_output(rendered) == value


# --------------------------------------------------------------------------- #
# Injection attempts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        "x'; touch /tmp/vars-injection-1; echo '",
        "x\"; touch /tmp/vars-injection-2; echo \"",
        "x`touch /tmp/vars-injection-3`",
        "x$(touch /tmp/vars-injection-4)",
        "x\ntouch /tmp/vars-injection-5",
        "x; touch /tmp/vars-injection-6;",
        "x && touch /tmp/vars-injection-7",
        "x | touch /tmp/vars-injection-8",
        "x > /tmp/vars-injection-9",
        "x\n\ntouch /tmp/vars-injection-10\n",
    ],
)
def test_command_injection_payloads_are_inert(payload: str, tmp_path) -> None:
    """The payload must be data, in every quoting context."""
    _require_bash()
    marker = tmp_path / "pwned"
    # Rebuild the payload against a path inside pytest's tmpdir.
    payload = payload.replace("/tmp/vars-injection-", str(marker) + "-")

    before = set(tmp_path.iterdir())
    for template in ("printf '%s' {{v}}", 'printf "%s" "{{v}}"', "printf '%s' '{{v}}'"):
        rendered = variables.render(template, {"v": payload}).text
        output = _bash_output(rendered)
        # Nothing the payload named may have been created.
        assert set(tmp_path.iterdir()) == before, (
            f"payload executed in context {template!r}: {payload!r}"
        )
        assert output == payload


def test_value_that_looks_like_a_flag_stays_one_argument() -> None:
    _require_bash()
    rendered = variables.render("printf '%s|' {{v}}", {"v": "--help"}).text
    assert _bash_output(rendered) == "--help|"


# --------------------------------------------------------------------------- #
# Comments and heredocs
# --------------------------------------------------------------------------- #


def test_reference_in_comment_is_substituted_without_escaping() -> None:
    _require_bash()
    rendered = variables.render("# host is {{h}}\necho hi", {"h": "web-01"})
    assert rendered.text == "# host is web-01\necho hi"
    assert _bash_output(rendered.text) == "hi\n"


def test_newline_in_comment_value_is_refused() -> None:
    with pytest.raises(variables.RenderError, match="newline"):
        variables.render("# host {{h}}", {"h": "a\ntouch /tmp/x"})


def test_hash_mid_word_is_not_a_comment() -> None:
    # `a#b` is one word, so the reference after it is in a bare-word context.
    rendered = variables.render("echo a#{{v}}", {"v": "x"})
    assert rendered.text == "echo a#'x'"


def test_reference_inside_heredoc_body_is_refused() -> None:
    script = "cat <<EOF\nvalue is {{v}}\nEOF\n"
    with pytest.raises(variables.RenderError, match="heredoc"):
        variables.render(script, {"v": "x"})


def test_heredoc_without_references_is_untouched() -> None:
    script = "cat <<EOF\nplain $HOME\nEOF\n"
    assert variables.render(script, {"v": "x"}).text == script


def test_reference_after_heredoc_still_renders() -> None:
    script = "cat <<EOF\nplain\nEOF\necho {{v}}\n"
    assert variables.render(script, {"v": "x"}).text == (
        "cat <<EOF\nplain\nEOF\necho 'x'\n"
    )


def test_heredoc_terminator_ends_the_body() -> None:
    script = "cat <<'EOF'\n{{v}}\nEOF\n"
    # Quoted delimiter still counts as a heredoc body: the reference is refused.
    with pytest.raises(variables.RenderError, match="heredoc"):
        variables.render(script, {"v": "x"})


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


def test_value_over_limit_is_refused() -> None:
    with pytest.raises(variables.RenderError, match="above the"):
        variables.render("{{v}}", {"v": "x" * (variables.MAX_VALUE_CHARS + 1)})


def test_rendered_output_over_limit_is_refused() -> None:
    chunk = "x" * 1000
    script = " ".join(["{{v}}"] * 1100)
    with pytest.raises(variables.RenderError, match="rendered script exceeds"):
        variables.render(script, {"v": chunk})


def test_empty_script_renders_to_empty() -> None:
    assert variables.render("", {"v": "x"}).text == ""


def test_script_without_references_is_unchanged() -> None:
    script = "set -eu\necho hello\n"
    assert variables.render(script, {"unused": "x"}).text == script


def test_referenced_names_helper() -> None:
    assert variables.referenced_names("echo {{a}} {{b}}") == {"a", "b"}
    assert variables.referenced_names("echo $HOME") == set()
