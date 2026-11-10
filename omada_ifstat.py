#!/usr/bin/env python3
"""
API mínima para estatísticas e status das interfaces do Omada ER605.

Uso:
  export OMADA_USER=admin
  export OMADA_PASS='...'
  python3 omada_ifstat.py            # sobe a API em 127.0.0.1:8080
  python3 omada_ifstat.py --once     # imprime status + estatísticas por WAN e sai
  python3 omada_ifstat.py --table    # mesmo conteúdo, em tabelas para leitura humana
  python3 omada_ifstat.py --raw      # imprime o JSON bruto do roteador e sai

Endpoints:
  GET /ifstat          -> "Statistics List" (Interface Statistics), todas as interfaces
  GET /ifstat/wan1     -> só WAN1   (idem wan2, lan ...)
  GET /status          -> "WAN IPv4" do System Status (tipo, link, IP, máscara,
                          MAC, gateway, DNS, Online Check)
  GET /status/wan2     -> só WAN2
  GET /wan             -> status + estatísticas juntos, por WAN
  GET /wan/wan1        -> só WAN1

Só usa a biblioteca padrão. Não envia nada para fora além do próprio roteador.
"""
import http.cookiejar
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("OMADA_URL", "https://omada.modolo.net.br")
USER = os.environ.get("OMADA_USER", "admin")
PASS = os.environ.get("OMADA_PASS", "")
# O certificado do roteador não bate com o hostname: por padrão não verifica.
# Use OMADA_VERIFY_TLS=1 para voltar a validar o certificado.
VERIFY_TLS = os.environ.get("OMADA_VERIFY_TLS", "0") == "1"

URL_IFSTAT = "/admin/ifstat?form=list"
URL_STATUS = "/admin/interface?form=status2"  # mesma fonte das tabelas "WAN IPv4"

# Rótulos de "Connection Type" exibidos pela interface web (campo t_proto)
PROTO_LABEL = {
    "static": "Static IP",
    "dhcp": "Dynamic IP",
    "pppoe": "PPPoE",
    "pptp": "PPTP",
    "l2tp": "L2TP",
    "bpa": "BPA",
    "3g": "3G/4G",
    "v6plus": "MAP-E",
    "dslite": "DS-Lite",
    "pppoa": "PPPoA",
    "ipoa": "IPoA",
}

_ctx = ssl.create_default_context()
if not VERIFY_TLS:
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE
# ProxyHandler({}) ignora HTTP(S)_PROXY herdados da sessão.
# O cookie jar guarda o cookie de sessão do login (sem ele o roteador responde 704).
_cookies = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_ctx),
    urllib.request.HTTPCookieProcessor(_cookies),
)
_lock = threading.Lock()
_stok = None

# Erros que significam "sessão perdida": 703/704 expirou ou token inválido, 711 a
# sessão foi encerrada por outro login (o ER605 aceita uma por vez) ou por logout;
# 401/403 e resposta não-JSON (ex.: página de login) são tratados igual pela
# interface web.
SESSION_ERRORS = {"401", "403", "703", "704", "711", "invalid_response"}
# Depois de um login recusado, espera antes de tentar de novo, para não esgotar as
# tentativas e bloquear a conta (709).
LOGIN_BACKOFF = int(os.environ.get("OMADA_LOGIN_BACKOFF", "60"))
# Depois de um 711 (alguém entrou pela interface web), espera antes de relogar.
# Relogar na hora derrubaria quem está usando o navegador.
YIELD_SECONDS = int(os.environ.get("OMADA_YIELD_SECONDS", "120"))
_blocked_until = 0.0
_blocked_reason = ""


class RouterUnavailable(RuntimeError):
    """Falha temporária: a API está esperando antes de tentar logar de novo."""

    def __init__(self, msg, retry_after):
        super().__init__(msg)
        self.retry_after = max(1, int(retry_after))


def _log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def _call(path, params=None, method="get", stok=""):
    """Chamada no protocolo do firmware: POST data={"method":..,"params":..}."""
    url = f"{BASE}/cgi-bin/luci/;stok={stok}{path}"
    op = {"get": "read", "set": "write"}.get(method, method)
    # Cada endpoint aceita só um dos dois estilos; o outro devolve error_code 1014.
    styles = (
        {"data": json.dumps({"method": method, "params": params or {}})},
        {"operation": op, **(params or {})},
    )
    if method == "get":  # tabelas (grid) usam operation=load no estilo antigo
        styles += ({"operation": "load", **(params or {})},)
    resp = {}
    for form in styles:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(form).encode(),
            headers={"Referer": BASE + "/webpages/index.html"},
        )
        try:
            with _opener.open(req, timeout=20) as r:
                body = r.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {"error_code": str(exc.code)}
            raise
        try:
            resp = json.loads(body)
        except ValueError:
            return {"error_code": "invalid_response"}
        if str(resp.get("error_code")) != "1014":
            break
    return resp


def _rsa_encrypt(text, n_hex, e_hex):
    """RSA "raw" igual ao encrypt.js do roteador (função nopadding).

    Os bytes do texto ficam no início do bloco e o resto é preenchido com zeros
    (sem padding PKCS#1). Resultado: hex com 2*k caracteres.
    """
    n, e = int(n_hex, 16), int(e_hex, 16)
    k = (n.bit_length() + 7) // 8
    ba = bytearray()
    for ch in text:  # reproduz a codificação do JS, inclusive a ordem dos bytes
        c = ord(ch)
        if c < 128:
            ba.append(c)
        elif c < 2048:
            ba += bytes([(c & 63) | 128, (c >> 6) | 192])
        else:
            ba += bytes([(c & 63) | 128, ((c >> 6) & 63) | 128, (c >> 12) | 224])
    if len(ba) > k:
        raise ValueError("senha longa demais")
    m = int.from_bytes(bytes(ba) + b"\x00" * (k - len(ba)), "big")
    return format(pow(m, e, n), "x").zfill(k * 2)


def _block(seconds, reason):
    global _blocked_until, _blocked_reason
    _blocked_until = time.monotonic() + seconds
    _blocked_reason = reason
    _log(f"{reason}; novo login em {seconds}s")
    raise RouterUnavailable(reason, seconds)


def login():
    """Faz login; em caso de recusa, bloqueia novas tentativas por um tempo."""
    global _stok
    if not PASS:
        raise RuntimeError("defina OMADA_PASS")
    _stok = None
    _cookies.clear()
    key = _call("/login?form=login").get("result") or {}
    lang = _call("/locale?form=lang").get("result") or {}
    if "password" not in key or "uptime" not in lang:
        raise RuntimeError("roteador não devolveu a chave de login")
    n_hex, e_hex = key["password"]
    enc = _rsa_encrypt(f"{PASS}_{lang['uptime']}", n_hex, e_hex)
    resp = _call(
        "/login?form=login", {"username": USER, "password": enc}, method="login"
    )
    code = str(resp.get("error_code"))
    if code != "0":
        detail = json.dumps(resp, ensure_ascii=False)
        if code == "709":  # tentativas esgotadas: espera mais
            _block(max(LOGIN_BACKOFF, 300), f"login bloqueado pelo roteador: {detail}")
        if code == "710":  # outra sessão ativa
            _block(YIELD_SECONDS, f"login recusado, usuário em conflito: {detail}")
        _block(LOGIN_BACKOFF, f"login falhou: {detail}")
    _stok = resp["result"]["stok"]
    if not len(_cookies):
        _log("aviso: o login não devolveu cookie de sessão")
    _log("login efetuado")


def logout():
    """Encerra a sessão no roteador (melhor esforço), liberando-a para o navegador."""
    global _stok
    with _lock:
        if _stok:
            try:
                _call("/admin/system?form=logout", method="set", stok=_stok)
                _log("logout efetuado")
            except Exception:  # noqa: BLE001 - saindo de qualquer forma
                pass
        _stok = None
        _cookies.clear()


def _read(path):
    """Lê um endpoint autenticado, refazendo o login se a sessão caiu."""
    global _stok
    with _lock:
        wait = _blocked_until - time.monotonic()
        if wait > 0:
            raise RouterUnavailable(_blocked_reason, wait)
        code = None
        for _attempt in (1, 2):
            if not _stok:
                login()
            resp = _call(path, stok=_stok)
            code = str(resp.get("error_code"))
            if code == "0":
                return resp.get("result")
            if code not in SESSION_ERRORS:
                raise RuntimeError(f"{path} falhou: error_code={code}")
            _stok = None
            if code == "711":  # outro login (ex.: navegador) ou logout encerrou a sessão
                _block(YIELD_SECONDS, "sessão encerrada pelo roteador (711): outro login ou logout")
            _log(f"sessão expirada ({code}); refazendo login")
        raise RuntimeError(f"{path}: sessão recusada mesmo após novo login ({code})")


def _is_lan(name):
    return isinstance(name, str) and "LAN" in name  # igual a $.su.func.isLan


def _ip(v, default="0.0.0.0"):
    return default if v in (None, "", "---") else v  # mesma regra da tela


# Campos do ifstat -> nomes das colunas da "Statistics List"
# (tx_bps/rx_bps do firmware, apesar do nome, são exibidos em KB/s)
IFSTAT_FIELDS = (
    ("tx_bps", "tx_rate_kbps"),
    ("rx_bps", "rx_rate_kbps"),
    ("tx_pps", "tx_packet_rate_pps"),
    ("rx_pps", "rx_packet_rate_pps"),
    ("tx_bytes", "total_tx_bytes"),
    ("rx_bytes", "total_rx_bytes"),
    ("tx_pkts", "total_tx_packets"),
    ("rx_pkts", "total_rx_packets"),
)


def read_ifstat():
    rows = []
    for item in _read(URL_IFSTAT) or []:
        row = {"zone": item.get("zone"), "interface": item.get("interface")}
        row.update({new: item.get(old) for old, new in IFSTAT_FIELDS})
        rows.append(row)
    return rows


def read_status(raw=None):
    """WANs do System Status (tabela "WAN IPv4"), com os nomes das colunas."""
    raw = _read(URL_STATUS) if raw is None else raw
    rows = []
    for item in (raw or {}).get("normal", []):
        name = item.get("t_name")
        if not name or _is_lan(name) or item.get("infobar") is False:
            continue
        rows.append(
            {
                "zone": name,  # WAN1, WAN2 (mesmo valor do "zone" do ifstat)
                "interface": item.get("t_label"),  # Interface Name: WAN1, WAN/LAN2
                "connection_type": PROTO_LABEL.get(item.get("t_proto"), "None"),
                "connection_status": "Link Up" if item.get("t_isup") is True else "Link Down",
                "ip_address": _ip(item.get("ipaddr")),
                "subnet_mask": _ip(item.get("netmask")),
                "mac_address": _ip(item.get("macaddr"), "00-00-00-00-00-00"),
                "default_gateway": _ip(item.get("gateway")),
                "primary_dns": _ip(item.get("dns1")),
                "online_check": "Online" if str(item.get("online_state")) == "1" else "Offline",
            }
        )
    return rows


def read_wan():
    """Status + estatísticas por WAN, unidos pela zona (WAN1, WAN2)."""
    stats = read_ifstat()
    out = []
    for st in read_status():
        stat = next((s for s in stats if s["zone"] == st["zone"]), None) or next(
            (s for s in stats if s["interface"] == st["interface"]), None
        )
        strip = lambda d: {k: v for k, v in (d or {}).items() if k not in ("zone", "interface")}
        out.append(
            {
                "zone": st["zone"],
                "interface": st["interface"],
                "status": strip(st),
                "statistics": strip(stat) if stat else None,
            }
        )
    return out


def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _matches(row, want):
    """Casa por zone (WAN1, WAN2) ou pelo nome da interface (WAN1, WAN/LAN2, LAN)."""
    w = _norm(want)
    return any(_norm(row.get(k, "")) == w for k in ("zone", "interface"))


SOURCES = {"ifstat": read_ifstat, "status": read_status, "wan": read_wan}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, headers=None):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if not parts or parts[0] not in SOURCES or len(parts) > 2:
            return self._send(
                404, {"error": "use /ifstat, /status ou /wan [/<interface>]"}
            )
        try:
            rows = SOURCES[parts[0]]() or []
        except RouterUnavailable as exc:  # esperando para relogar
            return self._send(
                503,
                {"error": str(exc), "retry_after": exc.retry_after},
                {"Retry-After": str(exc.retry_after)},
            )
        except Exception as exc:  # noqa: BLE001
            return self._send(502, {"error": str(exc)})
        if len(parts) == 2:
            want = parts[1]
            rows = [r for r in rows if _matches(r, want)]
            if not rows:
                return self._send(404, {"error": f"interface '{want}' não encontrada"})
        self._send(200, rows)

    def log_message(self, *_):
        pass


def _human_bytes(n):
    """Bytes em unidade legível (base 1024), ex.: 985985295 -> '940.3 MB'."""
    if not isinstance(n, (int, float)):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _render_table(title, headers, rows, right=()):
    """Tabela em texto com colunas alinhadas; índices em `right` vão à direita."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]

    def line(values):
        return "  ".join(
            v.rjust(w) if i in right else v.ljust(w)
            for i, (v, w) in enumerate(zip(values, widths))
        ).rstrip()

    out = [title, line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in cells] or ["(nenhuma interface)"]
    return "\n".join(out)


def format_tables(wans):
    status = [
        (w["interface"], s["connection_type"], s["connection_status"], s["ip_address"],
         s["subnet_mask"], s["mac_address"], s["default_gateway"], s["primary_dns"],
         s["online_check"])
        for w in wans for s in [w["status"]]
    ]
    stats = []
    for w in wans:
        s = w["statistics"] or {}
        stats.append((
            w["interface"],
            s.get("tx_rate_kbps", "-"), s.get("rx_rate_kbps", "-"),
            s.get("tx_packet_rate_pps", "-"), s.get("rx_packet_rate_pps", "-"),
            _human_bytes(s.get("total_tx_bytes")), _human_bytes(s.get("total_rx_bytes")),
            s.get("total_tx_packets", "-"), s.get("total_rx_packets", "-"),
        ))
    return "\n\n".join([
        _render_table(
            "WAN Status",
            ["Interface", "Connection Type", "Status", "IP Address", "Subnet Mask",
             "MAC Address", "Gateway", "Primary DNS", "Online Check"],
            status,
        ),
        _render_table(
            "WAN Statistics",
            ["Interface", "TX KB/s", "RX KB/s", "TX Pkt/s", "RX Pkt/s",
             "Total TX", "Total RX", "TX Packets", "RX Packets"],
            stats,
            right=range(1, 9),  # colunas numéricas
        ),
    ])


if __name__ == "__main__":
    # SIGTERM (ex.: systemd stop) segue o mesmo caminho do Ctrl+C: faz logout e sai
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        if "--raw" in sys.argv:  # JSON bruto do roteador, para depuração
            print(
                json.dumps(
                    {"ifstat": _read(URL_IFSTAT), "status2": _read(URL_STATUS)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif "--table" in sys.argv:  # visualização humana
            print(format_tables(read_wan()))
        elif "--once" in sys.argv:  # mesma saída do GET /wan
            print(json.dumps(read_wan(), ensure_ascii=False, indent=2))
        else:
            host = os.environ.get("LISTEN_HOST", "127.0.0.1")
            port = int(os.environ.get("LISTEN_PORT", "8080"))
            _log(f"API em http://{host}:{port}/  (/ifstat, /status, /wan)")
            ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:  # inclui RouterUnavailable
        _log(f"erro: {exc}")
        sys.exit(1)
    finally:
        logout()  # libera a sessão para quem usa a interface web
