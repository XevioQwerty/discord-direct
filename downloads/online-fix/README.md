# downloads/online-fix/

Online-Fix `.zip`s for games, hosted in-repo for quick downloading — handy when
someone owns the **real** copy and needs the same fix as everyone else to play
together (Online-Fix multiplayer is a separate P2P network; all players must
match).

## Files

| File | Game |
|------|------|
| `OnlineFix-Toggle.bat` | universal toggle script (any game) |
| `forza-horizon-online-fix.zip` | Forza Horizon *(upload)* |
| `gamble-with-friends-online-fix.zip` | Gamble With Your Friends *(upload)* |

## Raw link pattern

```
https://raw.githubusercontent.com/XevioQwerty/discord-direct/main/downloads/online-fix/<FILENAME>
```

## Using `OnlineFix-Toggle.bat`

1. Drop the `.bat` in your game's root folder (where the game `.exe` is).
2. Make a subfolder named `_OnlineFix` and extract the online-fix `.zip` into it.
3. Double-click the `.bat` → press **S** → enter the game's exe name → it creates
   two desktop shortcuts:
   - **`<Game> (Online Fix)`** — applies the fix, then launches the game.
   - **`<Game> (Normal)`** — restores your original files, then launches the game.

The script auto-backs-up any files it overwrites into `_OnlineFix_Backup`, so
switching back to real Steam is clean and reversible.

> Note: extract the fix files **flat** into `_OnlineFix` (the files that go in
> the game root), not inside an extra nested folder.
