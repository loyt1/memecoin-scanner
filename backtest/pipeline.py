"""Données du grand backtest sans biais, à lancer sur GitHub Actions (workflow « backtest »).

1. Toutes les migrations pump.fun → PumpSwap des 11 derniers jours (RPC public Solana) → liste des tokens gradués.
2. ATH de chaque token (API publique de pump.fun).
3. Bougies de 15 min (GeckoTerminal) des tokens dont l'ATH dépasse 800 k$.
4. Liquidité actuelle (Dexscreener) pour repérer les rugs par retrait de liquidité.
Reprend là où il s'est arrêté (fichiers dans etat/), et s'arrête proprement avant la limite de temps.
"""
import gzip, json, os, subprocess, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

ICI = os.path.dirname(os.path.abspath(__file__))
E = os.path.join(ICI, 'etat')
os.makedirs(E, exist_ok=True)
DEBUT = time.time()
LIMITE_S = float(os.environ.get('LIMITE_MIN', 320)) * 60
MIGRATION = '39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg'
RPC = 'https://api.mainnet-beta.solana.com'
SOL = 'So11111111111111111111111111111111111111112'
verrou = threading.Lock()


def temps_ok():
    return time.time() - DEBUT < LIMITE_S


def lire(nom, defaut):
    p = os.path.join(E, nom)
    if nom.endswith('.gz'):
        return json.load(gzip.open(p, 'rt')) if os.path.exists(p) else defaut
    return json.load(open(p)) if os.path.exists(p) else defaut


def ecrire(nom, obj):
    p = os.path.join(E, nom)
    with verrou:
        if nom.endswith('.gz'):
            json.dump(obj, gzip.open(p + '.tmp', 'wt'), separators=(',', ':'))
        else:
            json.dump(obj, open(p + '.tmp', 'w'), separators=(',', ':'))
        os.replace(p + '.tmp', p)


def req(url, data=None, ua='Mozilla/5.0', timeout=60):
    h = {'User-Agent': ua, 'Accept': 'application/json'}
    if data is not None:
        h['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout))


def rpc(methode, params):
    for i in range(8):
        try:
            r = req(RPC, {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params})
            if r.get('result') is not None:
                return r['result']
        except Exception:
            pass
        time.sleep(min(30, 2 ** i))
    return None


def sauvegarde_git(msg):
    """Commit de l'état sur la branche backtest (pour reprendre au lancement suivant)."""
    subprocess.run(['git', 'add', '-A', E], cwd=ICI)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ICI).returncode:
        subprocess.run(['git', 'commit', '-qm', msg], cwd=ICI)
        for _ in range(3):
            if subprocess.run(['git', 'push', '-q', 'origin', 'HEAD:backtest'], cwd=ICI).returncode == 0:
                break
            subprocess.run(['git', 'pull', '-q', '--rebase', 'origin', 'backtest'], cwd=ICI)


# ---------- 1. signatures des migrations ----------
def etape_signatures():
    sigs = lire('sigs.json', [])
    if sigs:
        return sigs
    coupe, avant = time.time() - 11 * 86400, None
    while True:
        o = {"limit": 1000}
        if avant:
            o["before"] = avant
        r = rpc("getSignaturesForAddress", [MIGRATION, o])
        if not r:
            break
        sigs += [[x['signature'], x['blockTime']] for x in r if not x['err']]
        avant = r[-1]['signature']
        if r[-1]['blockTime'] < coupe:
            break
    sigs = [s for s in sigs if s[1] >= coupe]
    ecrire('sigs.json', sigs)
    print('signatures', len(sigs), flush=True)
    return sigs


# ---------- 2. mint de chaque migration ----------
def mint_de(sig):
    t = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
    if t is None:
        return 'ECHEC'
    if not any('Migrate' in l for l in (t['meta'].get('logMessages') or [])):
        return None
    ms = [b['mint'] for b in t['meta'].get('postTokenBalances') or [] if b['mint'] != SOL]
    return ms[0] if ms else None


def etape_mints(sigs, MINTS, fini):
    a_faire = [(s, b) for s, b in sigs if MINTS.get(s, [None, 0])[0] == 'ECHEC' or s not in MINTS]
    n = 0
    with ThreadPoolExecutor(int(os.environ.get('RPC_THREADS', 6))) as ex:
        for (s, b), m in zip(a_faire, ex.map(lambda x: mint_de(x[0]) if temps_ok() else 'ECHEC', a_faire)):
            with verrou:
                MINTS[s] = [m, b]
            n += 1
            if n % 1000 == 0:
                ecrire('mints.json', MINTS)
                print('mints', n, '/', len(a_faire), time.strftime('%H:%M'), flush=True)
    ecrire('mints.json', MINTS)
    print('mints fini', sum(1 for v in MINTS.values() if v[0] and v[0] != 'ECHEC'), flush=True)
    fini.set()


# ---------- 3. ATH (pump.fun) ----------
def ath_de(m):
    for i in range(5):
        try:
            d = req(f'https://frontend-api-v3.pump.fun/coins-v2/{m}', timeout=30)
            if isinstance(d, dict) and d.get('mint'):
                return {k: d.get(k) for k in ('symbol', 'ath_market_cap', 'ath_market_cap_timestamp', 'pump_swap_pool',
                                              'usd_market_cap', 'created_timestamp')}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            time.sleep(5 * (i + 1))
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def etape_ath(MINTS, ATH, mints_fini, fini):
    n = 0
    while temps_ok():
        with verrou:
            a_faire = list({v[0] for v in MINTS.values() if v[0] and v[0] != 'ECHEC' and v[0] not in ATH})
        if not a_faire:
            if mints_fini.is_set():
                break
            time.sleep(20)
            continue
        with ThreadPoolExecutor(3) as ex:
            for m, d in zip(a_faire, ex.map(lambda m: (time.sleep(0.6), ath_de(m))[1] if temps_ok() else None, a_faire)):
                if d is not None:
                    with verrou:
                        ATH[m] = d
                n += 1
                if n % 500 == 0:
                    ecrire('ath.json', ATH)
                    print('ath', n, time.strftime('%H:%M'), flush=True)
    ecrire('ath.json', ATH)
    print('ath fini', len(ATH), flush=True)
    fini.set()


# ---------- 4. bougies (GeckoTerminal) ----------
def etape_bougies(ATH, OHLCV, ath_fini):
    n = 0
    while temps_ok():
        with verrou:
            a_faire = [(m, x) for m, x in ATH.items() if (x.get('ath_market_cap') or 0) >= 8e5 and x.get('pump_swap_pool')
                       and m not in OHLCV]
        if not a_faire:
            if ath_fini.is_set():
                break
            time.sleep(30)
            continue
        for m, x in a_faire:
            if not temps_ok():
                break
            d = None
            for i in range(4):
                try:
                    d = req(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{x['pump_swap_pool']}/ohlcv/minute"
                            f"?aggregate=15&limit=1000&currency=usd&token={m}", ua='memecoin-backtest')
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        d = {}
                        break
                    time.sleep(30 if e.code == 429 else 5)
                except Exception:
                    time.sleep(5)
            if d is not None:
                with verrou:
                    OHLCV[m] = (d.get('data') or {}).get('attributes', {}).get('ohlcv_list', [])
                n += 1
                if n % 100 == 0:
                    ecrire('ohlcv.json.gz', OHLCV)
                    print('bougies', n, time.strftime('%H:%M'), flush=True)
            time.sleep(float(os.environ.get('GT_PAUSE', 4)))
    ecrire('ohlcv.json.gz', OHLCV)
    print('bougies fini', len(OHLCV), flush=True)


def etape_liquidite(OHLCV):
    toks, L = list(OHLCV), {}
    for k in range(0, len(toks), 30):
        try:
            for p in req('https://api.dexscreener.com/tokens/v1/solana/' + ','.join(toks[k:k + 30])) or []:
                t = p['baseToken']['address']
                L[t] = max(L.get(t, 0), float((p.get('liquidity') or {}).get('usd') or 0))
        except Exception:
            pass
        time.sleep(0.3)
    ecrire('liq_gradues.json', {'t': int(time.time()), 'liq': L})


def main():
    sigs = etape_signatures()
    MINTS, ATH, OHLCV = lire('mints.json', {}), lire('ath.json', {}), lire('ohlcv.json.gz', {})
    mints_fini, ath_fini = threading.Event(), threading.Event()
    ts = [threading.Thread(target=etape_mints, args=(sigs, MINTS, mints_fini)),
          threading.Thread(target=etape_ath, args=(MINTS, ATH, mints_fini, ath_fini)),
          threading.Thread(target=etape_bougies, args=(ATH, OHLCV, ath_fini))]
    for t in ts:
        t.start()
    dernier = time.time()
    while any(t.is_alive() for t in ts):
        time.sleep(30)
        if time.time() - dernier > 1800:
            ecrire('mints.json', MINTS); ecrire('ath.json', ATH); ecrire('ohlcv.json.gz', OHLCV)
            sauvegarde_git('backtest : sauvegarde intermédiaire')
            dernier = time.time()
    complet = ath_fini.is_set() and temps_ok()
    if complet:
        etape_liquidite(OHLCV)
    sauvegarde_git('backtest : données ' + ('complètes' if complet else 'partielles'))
    print('COMPLET' if complet else 'PARTIEL (relancer le workflow pour continuer)', flush=True)
    sys.exit(0)


if __name__ == '__main__':
    main()
