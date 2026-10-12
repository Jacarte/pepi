#!/bin/bash
# Select a profile and overlay its immediate files onto this script's directory.
# A profile may contain settings.json, mcp.json and models-router.json; only
# files actually present are copied. Unrelated root files are left untouched.

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

script_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit 1
profiles_root="$script_root/profiles"
[[ -d "$profiles_root" && ! -L "$profiles_root" ]] || fail 'Missing profiles directory (or it is a symlink).'

# Globs preserve spaces and include hidden entries without parsing command output.
export LC_ALL=C
shopt -s nullglob dotglob
profiles=()
for entry in "$profiles_root"/*; do
    [[ -d "$entry" && ! -L "$entry" ]] && profiles+=("$entry")
done
[[ ${#profiles[@]} -gt 0 ]] || fail 'No profile directories found.'

terminal_state=''
staging=''
restore_terminal() {
    if [[ -n "$terminal_state" ]]; then
        stty "$terminal_state" || return 1
        terminal_state=''
    fi
}
cleanup() {
    restore_terminal
    [[ -z "$staging" ]] || rm -rf -- "$staging"
}
# One EXIT handler owns both terminal restoration and later staging cleanup.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 131' QUIT
if [[ -t 0 ]]; then
    terminal_state=$(stty -g) || fail 'Could not read terminal state.'
    stty -icanon -echo min 1 time 0 || fail 'Could not set terminal mode.'
fi

index=0
redraw=false
render_menu() {
    if [[ -t 0 && -t 1 ]]; then
        if $redraw; then printf '\033[%dA' "$((${#profiles[@]} + 1))"; fi
        for ((i = 0; i < ${#profiles[@]}; i++)); do
            printf '\r\033[2K'
            if [[ $i -eq $index ]]; then
                printf '\033[7m> %s\033[0m\n' "${profiles[i]##*/}"
            else
                printf '  %s\n' "${profiles[i]##*/}"
            fi
        done
        printf '\r\033[2K'
    else
        for ((i = 0; i < ${#profiles[@]}; i++)); do
            if [[ $i -eq $index ]]; then
                printf '> %s\n' "${profiles[i]##*/}"
            else
                printf '  %s\n' "${profiles[i]##*/}"
            fi
        done
    fi
    # Leave the cursor below the menu; it is never hidden.
    printf 'Up/Down to move, Enter to apply, q or Ctrl-C to cancel.\n'
    redraw=true
}

printf 'Available profiles:\n'
render_menu
while :; do
    if [[ -t 0 ]]; then
        # Bash 3.2 can defer a trapped signal while entering read. Polling bounds
        # that delay; it also reports timeout as status 1, just like EOF.
        if ! IFS= read -r -n 1 -t 1 key; then
            [[ -t 0 ]] || fail 'No selection received (EOF).'
            continue
        fi
    else
        IFS= read -r -n 1 key || fail 'No selection received (EOF).'
    fi
    case "$key" in
        ''|$'\r') selected=${profiles[index]}; break ;;
        q|Q) printf 'Cancelled.\n'; exit 0 ;;
        $'\003') exit 130 ;;
        $'\004') fail 'No selection received (EOF).' ;;
        $'\033')
            # Bash 3.2 supports integer timeouts. Bound each continuation read
            # so an isolated Escape returns to normal input instead of hanging.
            IFS= read -r -n 1 -t 1 prefix || continue
            [[ "$prefix" == '[' || "$prefix" == O ]] || continue
            sequence=''
            arrow=''
            for ((n = 0; n < 32; n++)); do
                IFS= read -r -n 1 -t 1 key || break
                [[ -n "$key" ]] || break
                if [[ "$key" == [@-~] ]]; then
                    # Consume unknown CSI/SS3 sequences, not their individual
                    # bytes. Only unmodified arrows change the selection.
                    [[ -z "$sequence" ]] && arrow=$key
                    break
                fi
                sequence+=$key
            done
            # Clamp at either end (also works for a single profile).
            case "$arrow" in
                A) if [[ $index -gt 0 ]]; then index=$((index - 1)); fi ;;
                B) if [[ $index -lt $((${#profiles[@]} - 1)) ]]; then index=$((index + 1)); fi ;;
                *) continue ;;
            esac
            render_menu
            ;;
        *) : ;; # Unknown keys, including digits, never select a profile.
    esac
done
restore_terminal || fail 'Could not restore terminal state.'

files=()
for entry in "$selected"/*; do
    # Do not recurse or follow source symlinks outside the selected profile.
    [[ -f "$entry" && ! -L "$entry" ]] && files+=("$entry")
done
[[ ${#files[@]} -gt 0 ]] || fail 'Selected profile contains no regular files.'

# Preflight all destinations before copying. In particular, cp must never
# follow an existing destination symlink, even a dangling one.
for file in "${files[@]}"; do
    destination="$script_root/${file##*/}"
    if [[ -L "$destination" || ( -e "$destination" && ! -f "$destination" ) ]]; then
        fail "Unsafe destination: ${file##*/} (not a regular file)."
    fi
done

# Stage fresh inodes before replacing files, so hard-linked destinations cannot
# modify profile sources or unrelated data. Copy failures leave root files intact.
staging=$(mktemp -d "$script_root/.profile-copy.XXXXXX") || fail 'Could not create staging directory.'
for file in "${files[@]}"; do
    cp -p "$file" "$staging/${file##*/}" || fail "Could not copy: ${file##*/}"
done
for file in "${files[@]}"; do
    mv -f "$staging/${file##*/}" "$script_root/${file##*/}" || fail "Could not replace: ${file##*/}"
done
printf 'Applied profile: %s\n' "${selected##*/}"
