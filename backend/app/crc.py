"""CRC16-CCITT (poly 0x1021, init 0xFFFF)，二进制帧校验码算法。

发送器端有一份独立实现（sender/frames.py），两端各自实现同一公开算法，
避免“发送器直接调用服务端代码”这种假接入。
"""


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc
