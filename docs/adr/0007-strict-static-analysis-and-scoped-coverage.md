# Maximally Strict Static Analysis; Coverage Gate Excludes the GUI

The project is stricter than its cpx400dp-gui template:
- **Tools:** Ruff with `select = ["ALL"]`, `mypy --strict`, Black, Bandit, pip-audit and pre-commit.
- **Allowed exceptions:** Ruff rules that conflict with Black. Docstring rules are limited to modules and public classes. Tests get relaxed rules, and so do Tk callbacks (boolean positional arguments).
- **pyvisa typing:** pyvisa is partly untyped, so it is wrapped in a typed boundary in the transport layer instead of spreading `Any` or ignores through the code.

pytest-cov fails the build below 85% for everything except `gui/`. GUI coverage is reported but not gated. GUI tests do run under xvfb and on Windows, but a coverage percentage for widget code mostly rewards hollow tests.
