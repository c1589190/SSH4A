import argparse
import getpass
import ipaddress
import json
import os
import sys
import warnings
from contextlib import contextmanager
from pathlib import Path

from . import VERSION, service
from .errors import AppError
from .profiles import (
    Connection,
    Profiles,
    alias,
    default_profile_name,
    fingerprint,
    hostname,
    port_number,
    username,
)
from .shell import require_terminal, run_terminal, send_secret
from .ssh import connect, positive_timeout, run_command
from .storage import config_path, write_status
from .tokens import Tokens
from .transfer import copy_file


def password_input(args) -> str | None:
    if getattr(args, "password_stdin", False):
        value = sys.stdin.buffer.read()
        value = (
            value.removesuffix(b"\r\n")
            if value.endswith(b"\r\n")
            else value.removesuffix(b"\n")
        )
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError("input", "Password input must be UTF-8") from exc
    if getattr(args, "ask_password", False):
        try:
            os.close(os.open("/dev/tty", os.O_RDWR))
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass()
        except KeyboardInterrupt:
            print(file=sys.stderr)
            raise
        except EOFError as exc:
            print(file=sys.stderr)
            raise AppError(
                "input", "Password entry ended before a password was read"
            ) from exc
        except (OSError, getpass.GetPassWarning) as exc:
            raise AppError(
                "input", "No terminal is available for hidden password entry"
            ) from exc
    return None


def add_password_options(parser, *, stdin: bool = True, reference: bool = False):
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--ask-password", action="store_true", help="read the password without echo"
    )
    if stdin:
        options.add_argument(
            "--password-stdin", action="store_true", help="read the password from stdin"
        )
    if reference:
        options.add_argument(
            "--password-ref", metavar="NAME", help="use the saved password from NAME"
        )


def add_connection_options(parser, *, stdin=False, save=False):
    parser.add_argument(
        "--profile", metavar="NAME", help="use NAME as the base connection profile"
    )
    host = parser.add_mutually_exclusive_group()
    host.add_argument("--host")
    host.add_argument("--host-ref", metavar="NAME")
    user = parser.add_mutually_exclusive_group()
    user.add_argument("--user")
    user.add_argument("--user-ref", metavar="NAME")
    parser.add_argument("--port", type=int)
    parser.add_argument("--connect-timeout", type=float, default=10, metavar="SECONDS")
    add_password_options(parser, stdin=stdin, reference=True)
    if save:
        parser.add_argument("--save-as", metavar="NAME")
        parser.add_argument("--save-password", action="store_true")


@contextmanager
def connected(args, store: Profiles):
    save_as = getattr(args, "save_as", None)
    save_password = getattr(args, "save_password", False)
    if save_password and not save_as:
        raise AppError("input", "--save-password requires --save-as")
    if save_as is not None:
        alias(save_as)
    target = store.resolve(
        Connection(
            profile=args.profile,
            host=args.host,
            host_ref=args.host_ref,
            user=args.user,
            user_ref=args.user_ref,
            port=args.port,
            password_ref=args.password_ref,
            password=password_input(args),
        )
    )
    with connect(target, args.connect_timeout) as transport:
        if save_as is not None:
            store.save(
                save_as,
                create=True,
                host=target.host,
                user=target.user,
                port=target.port,
                password=target.password if save_password else None,
            )
        yield transport


def profile_command(args, store: Profiles) -> int:
    if args.action in ("add", "update"):
        store.save(
            args.name,
            create=args.action == "add",
            host=args.host,
            user=args.user,
            port=args.port,
            password=password_input(args),
        )
    elif args.action == "delete":
        store.delete(args.name)
    elif args.action == "reveal":
        print(store.reveal(args.name, args.field))
    else:
        data = store.list() if args.action == "list" else store.show(args.name)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def split_host_port(address: str) -> tuple[str, str | None]:
    if address.startswith("["):
        host, separator, suffix = address[1:].partition("]")
        if separator and suffix.startswith(":"):
            return host, suffix[1:]
    elif address.count(":") == 1:
        host, port = address.split(":", 1)
        return host, port
    return address, None


def add_endpoint(address: str, option_port: int | None) -> tuple[str, int]:
    host, inline_port = split_host_port(address)
    if inline_port is None:
        return hostname(host), port_number(22 if option_port is None else option_port)
    if not (inline_port.isascii() and inline_port.isdecimal()):
        raise AppError("input", "The port after the colon must be a number")
    try:
        port = int(inline_port)
    except ValueError as exc:
        raise AppError("input", "Port must be between 1 and 65535") from exc
    port_number(port)
    if option_port is not None and option_port != port:
        raise AppError("input", "The address port and --port specify different values")
    return hostname(host), port


def add_command(args, store: Profiles) -> int:
    if args.name is not None:
        alias(args.name)
    positive_timeout(args.connect_timeout)
    host, port = add_endpoint(args.host, args.port)
    args.ask_password = not args.password_stdin
    target = store.resolve(
        Connection(
            host=host,
            port=port,
            user=username(args.user),
            password=password_input(args),
        )
    )
    name = args.name or default_profile_name(target)
    with connect(target, args.connect_timeout, allow_first_use=True) as transport:
        observed = fingerprint(transport.get_remote_server_key().asbytes())
        store.save(
            name,
            create=True,
            host=target.host,
            user=target.user,
            port=target.port,
            password=target.password,
            verified_fingerprint=observed,
        )
    print(f"SSH login succeeded. Saved as {name}.")
    return 0


def trust_command(args, store: Profiles) -> int:
    store.trust(args.host, args.port, args.fingerprint)
    return 0


def exec_command(args, store: Profiles) -> int:
    try:
        positive_timeout(args.timeout)
        with connected(args, store) as transport:
            status = run_command(
                transport,
                args.command,
                None if args.password_stdin else sys.stdin.buffer,
                sys.stdout.buffer,
                sys.stderr.buffer,
                args.timeout,
            )
    except AppError as exc:
        if args.status_file:
            write_status(args.status_file, exc.status())
        raise
    if args.status_file:
        write_status(args.status_file, {"kind": "remote_exit", "exit_code": status})
    return status


def shell_command(args, store: Profiles) -> int:
    require_terminal()
    with connected(args, store) as transport:
        return run_terminal(transport, store, args.session, args.connect_timeout)


def session_command(args, store: Profiles) -> int:
    send_secret(store, args.session_id, args.password_ref, args.enter)
    return 0


def tui_command(args, store: Profiles) -> int:
    require_terminal()
    profiles = store.list()
    if not profiles:
        raise AppError("profile", "Add a saved connection with ssh4a profile add first")
    for index, profile in enumerate(profiles, 1):
        print(
            f"{index:>3}  {profile['name']}  {profile['user']}@{profile['host']}:{profile['port']}"
        )
    while True:
        try:
            selected = input("Select a profile number, or q to exit: ").strip()
        except EOFError:
            return 0
        if selected.lower() in ("", "q"):
            return 0
        if (
            selected.isascii()
            and selected.isdecimal()
            and len(selected) <= len(str(len(profiles)))
        ):
            index = int(selected)
            if 1 <= index <= len(profiles):
                profile = profiles[index - 1]
                args.profile = profile["name"]
                args.ask_password = not profile["has_password"]
                return shell_command(args, store)
        print("Select a number from the list.", file=sys.stderr)


def transfer_command(args, store: Profiles) -> int:
    positive_timeout(args.timeout)
    with connected(args, store) as transport:
        copy_file(
            transport,
            args.operation,
            args.local,
            args.remote,
            args.overwrite,
            args.timeout,
        )
    return 0


def mcp_command(args, store: Profiles) -> int:
    if args.action == "serve":
        from .mcp_server import serve

        serve(store, args.port)
    elif args.action == "enable":
        service.enable(store.directory.path, args.port)
        print(service.endpoint(args.port))
        if service.status()["linger"] is False:
            print(
                "Enable user lingering with loginctl enable-linger for startup before login.",
                file=sys.stderr,
            )
    elif args.action == "disable":
        service.disable()
    elif args.action == "status":
        print(json.dumps(service.status(), indent=2))
    else:
        tokens = Tokens(store.directory.path)
        if args.token_action == "create":
            print(tokens.create(args.name))
        elif args.token_action == "revoke":
            tokens.revoke(args.name)
        else:
            print(json.dumps(tokens.list(), indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ssh4a",
        description="Headless SSH for agents. Run ssh4a IP[:PORT] USER to connect and save a profile.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--config-dir", metavar="PATH", help="select the user configuration directory"
    )
    commands = parser.add_subparsers(dest="operation")

    add = commands.add_parser(
        "add", help="test an SSH login and save its connection in one step"
    )
    add.set_defaults(handler=add_command)
    add.add_argument("host", metavar="HOST[:PORT]")
    add.add_argument("user")
    add.add_argument("--port", type=int, help="SSH port (default 22)")
    add.add_argument("--name", metavar="NAME", help="choose the saved profile name")
    add.add_argument("--connect-timeout", type=float, default=10, metavar="SECONDS")
    add.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )

    profile = commands.add_parser("profile", help="manage saved connections")
    profile.set_defaults(handler=profile_command)
    actions = profile.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list saved profiles")
    for action in ("add", "update"):
        edit = actions.add_parser(action)
        edit.add_argument("name")
        edit.add_argument("--host", required=action == "add")
        edit.add_argument("--user", required=action == "add")
        edit.add_argument("--port", type=int)
        add_password_options(edit)
    for action in ("show", "delete"):
        actions.add_parser(action).add_argument("name")
    reveal = actions.add_parser(
        "reveal", help="explicitly print one field, including its plaintext password"
    )
    reveal.add_argument("name")
    reveal.add_argument("field", choices=("host", "user", "port", "password"))

    trust = commands.add_parser("trust", help="manage trusted SSH host fingerprints")
    trust_actions = trust.add_subparsers(dest="action", required=True)
    trust_set = trust_actions.add_parser("set")
    trust_set.set_defaults(handler=trust_command)
    trust_set.add_argument("--host", required=True)
    trust_set.add_argument("--port", type=int, default=22)
    trust_set.add_argument("--fingerprint", required=True)

    execute = commands.add_parser("exec", help="run one command without a PTY")
    execute.set_defaults(handler=exec_command)
    add_connection_options(execute, stdin=True, save=True)
    execute.add_argument("--timeout", type=float, default=60, metavar="SECONDS")
    execute.add_argument("--status-file", type=Path)
    execute.add_argument("command", metavar="COMMAND")
    shell = commands.add_parser(
        "shell", help="open a persistent interactive remote terminal"
    )
    shell.set_defaults(handler=shell_command)
    add_connection_options(shell, save=True)
    shell.add_argument(
        "--session", metavar="ID", help="name the session for local control"
    )

    tui = commands.add_parser(
        "tui", help="pick a saved connection from a terminal menu"
    )
    tui.set_defaults(
        handler=tui_command,
        host=None,
        host_ref=None,
        user=None,
        user_ref=None,
        port=None,
        password_ref=None,
        session=None,
        connect_timeout=10,
    )

    session = commands.add_parser(
        "session", help="control a named local interactive session"
    )
    session_actions = session.add_subparsers(dest="action", required=True)
    send = session_actions.add_parser("send-secret")
    send.set_defaults(handler=session_command)
    send.add_argument("session_id")
    send.add_argument("--password-ref", required=True, metavar="NAME")
    send.add_argument("--enter", action="store_true")

    for operation, operands in (
        ("put", ("local", "remote")),
        ("get", ("remote", "local")),
    ):
        transfer = commands.add_parser(
            operation, help=f"{operation} one file over SFTP"
        )
        transfer.set_defaults(handler=transfer_command)
        add_connection_options(transfer)
        transfer.add_argument("--overwrite", action="store_true")
        transfer.add_argument("--timeout", type=float, default=120, metavar="SECONDS")
        for operand in operands:
            transfer.add_argument(operand, type=Path if operand == "local" else str)
    mcp = commands.add_parser(
        "mcp", help="manage the local MCP service and Agent access"
    )
    mcp.set_defaults(handler=mcp_command)
    actions = mcp.add_subparsers(dest="action", required=True)
    for action in ("serve", "enable"):
        actions.add_parser(action).add_argument(
            "--port", type=int, default=service.DEFAULT_PORT
        )
    actions.add_parser("status")
    actions.add_parser("disable")
    token = actions.add_parser("token", help="manage independent Agent access tokens")
    token_actions = token.add_subparsers(dest="token_action", required=True)
    token_actions.add_parser("list")
    for action in ("create", "revoke"):
        token_actions.add_parser(action).add_argument("name")
    return parser


def expand_ip_shorthand(argv: list[str]) -> list[str]:
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--config-dir":
            index += 2
            continue
        if value.startswith("--config-dir="):
            index += 1
            continue
        host, _ = split_host_port(value)
        try:
            ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
        except ValueError:
            return argv
        return [*argv[:index], "add", *argv[index:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(
        expand_ip_shorthand(sys.argv[1:] if argv is None else argv)
    )
    if args.operation is None:
        parser.print_help()
        return 0
    try:
        return args.handler(args, Profiles(config_path(args.config_dir)))
    except AppError as exc:
        print(f"[ssh4a:{exc.kind}] {exc}", file=sys.stderr)
        return 255
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"[ssh4a:io] {exc.strerror}", file=sys.stderr)
        return 255


if __name__ == "__main__":
    sys.exit(main())
