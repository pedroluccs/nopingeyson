"""Biblioteca de jogos: nome e executáveis conhecidos de cada jogo.

O usuário pode adicionar jogos próprios (aba "Pedir jogo novo"), que ficam na
configuração em "custom_games" com o mesmo formato.
"""

CATALOG = [
    {"id": "valorant", "name": "VALORANT", "genre": "FPS",
     "exes": ["valorant-win64-shipping.exe"]},
    {"id": "cs2", "name": "Counter-Strike 2", "genre": "FPS", "exes": ["cs2.exe"]},
    {"id": "lol", "name": "League of Legends", "genre": "MOBA",
     "exes": ["league of legends.exe"]},
    {"id": "dota2", "name": "Dota 2", "genre": "MOBA", "exes": ["dota2.exe"]},
    {"id": "fortnite", "name": "Fortnite", "genre": "Battle Royale",
     "exes": ["fortniteclient-win64-shipping.exe"]},
    {"id": "apex", "name": "Apex Legends", "genre": "Battle Royale",
     "exes": ["r5apex.exe", "r5apex_dx12.exe"]},
    {"id": "pubg", "name": "PUBG: Battlegrounds", "genre": "Battle Royale",
     "exes": ["tslgame.exe"]},
    {"id": "warzone", "name": "Call of Duty", "genre": "FPS", "exes": ["cod.exe", "cod24.exe"]},
    {"id": "r6", "name": "Rainbow Six Siege", "genre": "FPS",
     "exes": ["rainbowsix.exe", "rainbowsix_vulkan.exe", "rainbowsix_be.exe"]},
    {"id": "overwatch", "name": "Overwatch 2", "genre": "FPS", "exes": ["overwatch.exe"]},
    {"id": "rocket", "name": "Rocket League", "genre": "Esportes",
     "exes": ["rocketleague.exe"]},
    {"id": "fc", "name": "EA SPORTS FC", "genre": "Esportes",
     "exes": ["fc25.exe", "fc26.exe"]},
    {"id": "gta5", "name": "GTA V Online", "genre": "Mundo aberto",
     "exes": ["gta5.exe", "gta5_enhanced.exe"]},
    {"id": "rust", "name": "Rust", "genre": "Sobrevivência", "exes": ["rustclient.exe"]},
    {"id": "tarkov", "name": "Escape from Tarkov", "genre": "FPS",
     "exes": ["escapefromtarkov.exe"]},
    {"id": "wow", "name": "World of Warcraft", "genre": "MMO", "exes": ["wow.exe"]},
    {"id": "lostark", "name": "Lost Ark", "genre": "MMO", "exes": ["lostark.exe"]},
    {"id": "albion", "name": "Albion Online", "genre": "MMO",
     "exes": ["albion-online.exe"]},
    {"id": "ffxiv", "name": "Final Fantasy XIV", "genre": "MMO", "exes": ["ffxiv_dx11.exe"]},
    {"id": "poe", "name": "Path of Exile", "genre": "RPG",
     "exes": ["pathofexile.exe", "pathofexile_x64.exe", "pathofexilesteam.exe"]},
    {"id": "deadlock", "name": "Deadlock", "genre": "MOBA", "exes": ["deadlock.exe"]},
    {"id": "marvelrivals", "name": "Marvel Rivals", "genre": "FPS",
     "exes": ["marvel-win64-shipping.exe"]},
    {"id": "thefinals", "name": "THE FINALS", "genre": "FPS", "exes": ["discovery.exe"]},
    {"id": "pointblank", "name": "Point Blank", "genre": "FPS", "exes": ["pointblank.exe"]},
    {"id": "freefire", "name": "Free Fire (emulador)", "genre": "Battle Royale",
     "exes": ["hd-player.exe"]},
    {"id": "minecraft", "name": "Minecraft (Bedrock)", "genre": "Sandbox",
     "exes": ["minecraft.windows.exe"]},
    {"id": "roblox", "name": "Roblox", "genre": "Sandbox", "exes": ["robloxplayerbeta.exe"]},
    {"id": "brawlhalla", "name": "Brawlhalla", "genre": "Luta", "exes": ["brawlhalla.exe"]},
    {"id": "tekken8", "name": "TEKKEN 8", "genre": "Luta",
     "exes": ["polaris-win64-shipping.exe"]},
    {"id": "sf6", "name": "Street Fighter 6", "genre": "Luta", "exes": ["streetfighter6.exe"]},
]


def all_games(cfg: dict) -> list[dict]:
    custom = cfg.get("custom_games") or []
    ids = {g["id"] for g in custom}
    return [g for g in CATALOG if g["id"] not in ids] + [dict(g, custom=True) for g in custom]


def by_exe(cfg: dict) -> dict[str, dict]:
    out = {}
    for g in all_games(cfg):
        for exe in g["exes"]:
            out[exe.lower()] = g
    return out
