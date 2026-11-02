"""HIS 接口适配骨架 —— HL7 v2 over MLLP 监听器。

适用前提
--------
只有当 HIS 采用「主动推送」模式时，我们才需要监听端口。
其余三种形态（HTTP 接口、数据库视图、文件交换）都是我们主动去连对方，
本文件的 MllpServer 用不上。

报文长什么样
------------
MLLP 是 HL7 的传输封装，一帧 = 起始符 + 报文体 + 结束符：

    <VT>  HL7 报文体  <FS><CR>
    \\x0b              \\x1c\\x0d

报文体内部，每个「段」用 \\r 分隔，字段用 | 分隔：

    MSH|^~\\&|HIS|HOSP|EXAM|HOSP|202610312330||OMG^O19|MSG00001|P|2.5
    PID|1||P20261031001^^^HOSP^MR||张^三||20200101|M
    ORC|NW|ORD12345||||||202610312330
    OBR|1|ORD12345||CBC^血常规^L|||202610312330

两处最容易踩的坑
----------------
1. MSH 段是特殊的：MSH-1 就是字段分隔符本身，所以按 | 切分后
   MSH-3 在索引 2，MSH-10（消息控制号）在索引 9，不是 10。
2. 必须回 ACK，且 MSA 段要带回原消息的控制号。不回或回错，
   对方会认为投递失败并持续重发，队列里会堆满重复消息。

端口
----
2575 是 MLLP 的社区惯例端口，不是强制标准，以医院实际配置为准。
"""

from __future__ import annotations

import argparse
import socket
import threading
from datetime import datetime
from typing import Callable, List, Optional

VT = b"\x0b"          # 帧起始
FS_CR = b"\x1c\x0d"   # 帧结束
DEFAULT_PORT = 2575
ENCODINGS = ("utf-8", "gbk")   # 国内医院不少仍用 GBK，先试 UTF-8 再退回


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def decode_frame(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def split_segments(text: str) -> List[str]:
    normalized = text.replace("\r\n", "\r").replace("\n", "\r")
    return [s for s in normalized.split("\r") if s.strip()]


def _field(fields: List[str], index: int, default: str = "") -> str:
    return fields[index] if len(fields) > index else default


def parse_message(text: str) -> dict:
    """把一条 HL7 报文解析成本项目需要的最小结构。"""
    result = {
        "message_type": "",
        "control_id": "",
        "sending_app": "",
        "patient_id": "",
        "orders": [],
    }
    current: Optional[dict] = None

    for segment in split_segments(text):
        fields = segment.split("|")
        tag = fields[0]

        if tag == "MSH":
            result["sending_app"] = _field(fields, 2)
            result["message_type"] = _field(fields, 8)
            result["control_id"] = _field(fields, 9)
        elif tag == "PID":
            result["patient_id"] = _field(fields, 3).split("^")[0]
        elif tag == "ORC":
            current = {"order_no": _field(fields, 2)}
            result["orders"].append(current)
        elif tag == "OBR":
            parts = _field(fields, 4).split("^")
            if current is None:
                current = {"order_no": _field(fields, 2)}
                result["orders"].append(current)
            current["exam_code"] = parts[0] if parts else ""
            current["exam_name"] = parts[1] if len(parts) > 1 else ""
            current["requested_at"] = _field(fields, 7)
            current = None

    return result


def build_ack(control_id: str, sending_app: str = "HIS", sending_facility: str = "HOSP",
              ack_code: str = "AA") -> str:
    """构造 ACK。ack_code: AA=接收成功, AE=出错, AR=拒绝。"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    ack_id = f"ACK{ts}{control_id[-4:] if control_id else ''}"
    return (
        f"MSH|^~\\&|EXAMSCHED|HOSP|{sending_app}|{sending_facility}|{ts}||ACK|{ack_id}|P|2.5\r"
        f"MSA|{ack_code}|{control_id}\r"
    )


# ----------------------------------------------------------------------
# 监听
# ----------------------------------------------------------------------
class MllpServer:
    """最小可用的 MLLP 服务端：收帧、解析、回 ACK。"""

    def __init__(self, host: str, port: int, on_message: Callable[[dict], None],
                 verbose: bool = True) -> None:
        self.host = host
        self.port = port
        self.on_message = on_message
        self.verbose = verbose
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def _log(self, text: str) -> None:
        if self.verbose:
            print(f"[{datetime.now():%H:%M:%S}] {text}")

    def _handle(self, conn: socket.socket, addr) -> None:
        self._log(f"连接建立 {addr[0]}:{addr[1]}")
        buffer = b""
        with conn:
            while True:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                buffer += chunk

                # 一帧 = VT ... FS CR；TCP 可能把多帧粘在一起，也可能把一帧拆开
                while True:
                    start = buffer.find(VT)
                    end = buffer.find(FS_CR)
                    if start == -1 or end == -1 or end < start:
                        break
                    body = buffer[start + 1:end]
                    buffer = buffer[end + len(FS_CR):]

                    text = decode_frame(body)
                    if self.verbose:
                        first = split_segments(text)[0] if split_segments(text) else ""
                        self._log(f"收到 {len(body)} 字节 | {first[:60]}")

                    try:
                        parsed = parse_message(text)
                    except Exception as exc:                      # 解析失败要回 AE
                        self._log(f"解析失败：{exc}")
                        conn.sendall(VT + build_ack("", ack_code="AE").encode("utf-8") + FS_CR)
                        continue

                    conn.sendall(VT + build_ack(parsed["control_id"],
                                                parsed["sending_app"] or "HIS").encode("utf-8") + FS_CR)
                    self._log(f"已回 ACK | 控制号 {parsed['control_id']} | 医嘱 {len(parsed['orders'])} 条")
                    self.on_message(parsed)
        self._log(f"连接关闭 {addr[0]}:{addr[1]}")

    def serve_forever(self) -> None:
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._log(f"MLLP 监听中 {self.host}:{self.port}")
        try:
            while True:
                conn, addr = self._sock.accept()
                threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
        finally:
            self._sock.close()


# ----------------------------------------------------------------------
# 测试用的模拟发送端
# ----------------------------------------------------------------------
SAMPLE = (
    "MSH|^~\\&|HIS|HOSP|EXAM|HOSP|202610312330||OMG^O19|MSG00001|P|2.5\r"
    "PID|1||P20261031001^^^HOSP^MR||张^三||20200101|M\r"
    "ORC|NW|ORD12345||||||202610312330\r"
    "OBR|1|ORD12345||CBC^血常规^L|||202610312330\r"
    "ORC|NW|ORD12346||||||202610312330\r"
    "OBR|2|ORD12346||ECG^心电图^L|||202610312330\r"
    "ORC|NW|ORD12347||||||202610312330\r"
    "OBR|3|ORD12347||ECHO^心脏彩超^L|||202610312330\r"
)


def send_mllp(host: str, port: int, message: str, timeout: float = 3.0) -> str:
    """按 MLLP 封装发送一条报文，返回对方回的 ACK 文本。"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(VT + message.encode("utf-8") + FS_CR)
        data = sock.recv(8192)
    return decode_frame(data.replace(VT, b"").replace(FS_CR, b""))


def _print_parsed(msg: dict) -> None:
    print("  ┌ 解析结果")
    print(f"  │ 消息类型 {msg['message_type']}   控制号 {msg['control_id']}")
    print(f"  │ 患者 {msg['patient_id']}   开单科室 {msg['sending_app']}")
    for order in msg["orders"]:
        print(f"  │ 医嘱 {order.get('order_no')} → {order.get('exam_name')}"
              f"（{order.get('exam_code')}）")
    print("  └")


def main() -> None:
    parser = argparse.ArgumentParser(description="HIS HL7 MLLP 适配器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认所有网卡")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mock", action="store_true", help="自测：起服务并发一条样例报文")
    args = parser.parse_args()

    def on_message(msg: dict) -> None:
        _print_parsed(msg)

    server = MllpServer(args.host, args.port, on_message)

    if args.mock:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        import time
        time.sleep(0.5)

        print("\n=== 发送样例报文（含 3 条医嘱）===")
        ack = send_mllp("127.0.0.1", args.port, SAMPLE)
        print("=== 对方回的 ACK ===")
        for line in split_segments(ack):
            print("  " + line)
        time.sleep(0.5)
        return

    server.serve_forever()


if __name__ == "__main__":
    main()
