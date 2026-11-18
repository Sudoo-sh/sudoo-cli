"""Shell completion scripts, made from the command line's own definition (so they never go out of date)."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import Context

Tree = dict[str, tuple[list[str], dict[str, list[str]]]]  # command -> (its options, {action: options})


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _options(parser: argparse.ArgumentParser) -> list[str]:
    return sorted({o for a in parser._actions for o in a.option_strings if o.startswith("--")})


def tree(root: argparse.ArgumentParser) -> Tree:
    result: Tree = {}
    for name, sub in _subparsers(root).items():
        actions = _subparsers(sub)
        result[name] = (_options(sub), {a: _options(p) for a, p in actions.items()})
    return result


def bash(root: argparse.ArgumentParser) -> str:
    t = tree(root)
    lines = [
        '# sudoo completion for bash: eval "$(sudoo completion bash)" in ~/.bashrc',
        "_sudoo() {",
        '  local cur="${COMP_WORDS[COMP_CWORD]}" cmd="${COMP_WORDS[1]}" sub="${COMP_WORDS[2]}" words=""',
        '  if [ "$COMP_CWORD" -eq 1 ]; then',
        f'    words="{" ".join(sorted(t))} {" ".join(_options(root))}"',
        "  else",
        '    case "$cmd" in',
    ]
    for name, (options, actions) in sorted(t.items()):
        if actions:
            lines.append(f"      {name})")
            lines.append('        if [ "$COMP_CWORD" -eq 2 ]; then words="' + " ".join(sorted(actions)) + '"; else')
            lines.append('          case "$sub" in')
            for action, opts in sorted(actions.items()):
                lines.append(f'            {action}) words="{" ".join(opts)}" ;;')
            lines.append("          esac")
            lines.append("        fi ;;")
        else:
            lines.append(f'      {name}) words="{" ".join(options)}" ;;')
    lines += [
        "    esac",
        "  fi",
        '  COMPREPLY=($(compgen -W "$words" -- "$cur"))',
        "}",
        "complete -o default -F _sudoo sudoo",
        "",
    ]
    return "\n".join(lines)


def zsh(root: argparse.ArgumentParser) -> str:
    return (
        '# sudoo completion for zsh: eval "$(sudoo completion zsh)" in ~/.zshrc\n'
        "autoload -U +X compinit && compinit\n"
        "autoload -U +X bashcompinit && bashcompinit\n" + bash(root).split("\n", 1)[1]
    )


def fish(root: argparse.ArgumentParser) -> str:
    t = tree(root)
    commands = " ".join(sorted(t))
    lines = [
        "# sudoo completion for fish: sudoo completion fish > ~/.config/fish/completions/sudoo.fish",
        "complete -c sudoo -f",
        f'complete -c sudoo -n "not __fish_seen_subcommand_from {commands}" -a "{commands}"',
    ]
    for name, (options, actions) in sorted(t.items()):
        if actions:
            names = " ".join(sorted(actions))
            lines.append(
                f'complete -c sudoo -n "__fish_seen_subcommand_from {name}; and not __fish_seen_subcommand_from {names}" -a "{names}"'
            )
            for action, opts in sorted(actions.items()):
                for opt in opts:
                    lines.append(
                        f'complete -c sudoo -n "__fish_seen_subcommand_from {name}; and __fish_seen_subcommand_from {action}" -l {opt[2:]}'
                    )
        else:
            for opt in options:
                lines.append(f'complete -c sudoo -n "__fish_seen_subcommand_from {name}" -l {opt[2:]}')
    return "\n".join(lines) + "\n"


def run(ctx: Context, root: argparse.ArgumentParser) -> None:
    script = {"bash": bash, "zsh": zsh, "fish": fish}[ctx.args.shell](root)
    print(script, end="")
