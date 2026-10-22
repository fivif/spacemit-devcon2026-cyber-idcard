"""CLIP BPE 分词器（自包含实现，无第三方依赖）。

用途：把提示词（如 "person"）编码成 MobileCLIP 文本编码器的 input_ids。
对齐 SpacemiT 官方实现 src/deploy/yoloe/cpp/mobileclip_tokenizer.cc 的行为。

词表文件：assets/clip/bpe_merges.txt（标准 CLIP BPE merges，48894 条）。
"""

from __future__ import annotations

import gzip
import re
from functools import lru_cache
from pathlib import Path

CONTEXT_LENGTH = 77
SOT = 49406
EOT = 49407


@lru_cache(maxsize=1)
def bytes_to_unicode() -> dict[int, str]:
    """CLIP 的字节 -> unicode 映射（可逆，避免控制字符进 BPE）。"""
    bs = list(range(ord("!"), ord("~") + 1))
    bs += list(range(ord("\u00a1"), ord("\u00ac") + 1))
    bs += list(range(ord("\u00ae"), ord("\u00ff") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))


class ClipTokenizer:
    """把文本编码成 CLIP 词 id 序列（定长 77，SOT/EOT 包裹，0 填充）。"""

    def __init__(self, bpe_path: str | Path):
        self.byte_encoder = bytes_to_unicode()
        self.byte_decoder = {v: k for k, v in self.byte_encoder.items()}

        raw = Path(bpe_path)
        opener = gzip.open if raw.suffix == ".gz" else open
        with opener(raw, "rt", encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        # 兼容两种落盘格式：带 "#version" 头的 CLIP 原始词表，或裸 merges 文件。
        merges = []
        for line in lines:
            line = line.rstrip()
            if not line or "#" in line and "version" in line.lower():
                continue
            parts = line.split()
            if len(parts) == 2:
                merges.append((parts[0], parts[1]))

        vocab = list(self.byte_encoder.values())
        vocab += [v + "</w>" for v in self.byte_encoder.values()]
        for a, b in merges:
            vocab.append(a + b)
        vocab += ["<|startoftext|>", "<|endoftext|>"]

        self.encoder = dict(zip(vocab, range(len(vocab))))
        self.bpe_ranks = dict(zip(merges, range(len(merges))))
        self.cache = {"<|startoftext|>": "<|startoftext|>", "<|endoftext|>": "<|endoftext|>"}
        self.pat = re.compile(
            r"<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[a-zA-Z]+|[0-9]"
            r"|[^a-zA-Z0-9\s]+",
            re.IGNORECASE,
        )

    @staticmethod
    def _pairs(word: tuple[str, ...]) -> set[tuple[str, str]]:
        return {(word[i], word[i + 1]) for i in range(len(word) - 1)}

    def _bpe(self, token: str) -> str:
        if token in self.cache:
            return self.cache[token]
        word = tuple(token[:-1]) + (token[-1] + "</w>",)
        pairs = self._pairs(word)
        if not pairs:
            return token + "</w>"

        while True:
            bigram = min(pairs, key=lambda p: self.bpe_ranks.get(p, float("inf")))
            if bigram not in self.bpe_ranks:
                break
            first, second = bigram
            new_word: list[str] = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                except ValueError:
                    new_word.extend(word[i:])
                    break
                new_word.extend(word[i:j])
                i = j
                if word[i] == first and i < len(word) - 1 and word[i + 1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            word = tuple(new_word)
            if len(word) == 1:
                break
            pairs = self._pairs(word)

        result = " ".join(word)
        self.cache[token] = result
        return result

    @staticmethod
    def _basic_clean(text: str) -> str:
        return text.strip()

    @staticmethod
    def _whitespace_clean(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def encode(self, text: str) -> list[int]:
        """文本 -> 词 id（不含 SOT/EOT，不填充）。"""
        text = self._whitespace_clean(self._basic_clean(text)).lower()
        ids: list[int] = []
        for token in re.findall(self.pat, text):
            token = "".join(self.byte_encoder[b] for b in token.encode("utf-8"))
            ids.extend(self.encoder[piece] for piece in self._bpe(token).split(" "))
        return ids

    def encode_padded(self, text: str, context_length: int = CONTEXT_LENGTH) -> list[int]:
        """文本 -> 定长 77 的 id 序列（SOT ... EOT 0 填充），即文本编码器的输入。"""
        body = self.encode(text)[: context_length - 2]
        ids = [SOT, *body, EOT]
        ids += [0] * (context_length - len(ids))
        return ids