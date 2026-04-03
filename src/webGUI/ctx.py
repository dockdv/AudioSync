#!/usr/bin/env python3

from probe import get_duration
from audio import AUDIO_HOP_SEC, AUDIO_MAX_SAMPLES


def _speed_to_atempo(a):
    return 1.0 / a if abs(a) > 1e-9 else 1.0


class AlignContext:
    def __init__(self, fp1, fp2, track1, track2,
                 v1_probe, v2_probe, vocal_filter,
                 progress_cb, cancel):
        self.fp1 = fp1
        self.fp2 = fp2
        self.track1 = track1
        self.track2 = track2
        self.v1_info = v1_probe or {}
        self.v2_info = v2_probe or {}
        self.vocal_filter = vocal_filter
        self.progress_cb = progress_cb
        self.cancel = cancel

        self.dur1 = self.v1_info.get("duration", 0) or get_duration(fp1)
        self.dur2 = self.v2_info.get("duration", 0) or get_duration(fp2)
        hop = AUDIO_HOP_SEC
        max_s = AUDIO_MAX_SAMPLES
        self.hop1 = self.dur1 / max_s if (self.dur1 > 0 and self.dur1 / hop > max_s) else hop
        self.hop2 = self.dur2 / max_s if (self.dur2 > 0 and self.dur2 / hop > max_s) else hop
        self.max_s = max_s

        self.v1_has_video = any(s.get("codec_type") == "video"
                                for s in self.v1_info.get("streams", []))
        self.v2_has_video = any(s.get("codec_type") == "video"
                                for s in self.v2_info.get("streams", []))

        self.audio1 = None
        self.audio2 = None
        self.ts1 = None
        self.ts2 = None
        self.fp1_main = None
        self.fp2_main = None
        self.v1_lufs = None
        self.v2_lufs = None
        self.ah1 = None
        self.ah2 = None
        self.decode_warnings = []

        self.coarse_offset = 0.0
        self.xcorr_speed = 1.0
        self.audio_offset = 0.0
        self.audio_speed = 1.0
        self.alt_offsets = []
        self.ds1_seg = None
        self.ds2_seg = None
        self.ds_rate = 0.0
        self.visual_corrected = False
        self.visual_result = None

        self.mode = ""
        self.a = 1.0
        self.b = 0.0
        self.ni = 0
        self.total_good = 0
        self.pairs = []
        self.rmean = 0.0
        self.rmax = 0.0
        self.rend = 0.0
        self.segments = []

    def free_audio(self):
        self.audio1 = None
        self.audio2 = None

    def build_result(self):
        atempo = _speed_to_atempo(self.a)
        vr = self.visual_result
        vc = self.visual_corrected
        return {
            "speed_ratio": atempo, "offset": self.b,
            "linear_a": self.a, "linear_b": self.b,
            "inlier_count": self.ni, "total_candidates": self.total_good,
            "inlier_pairs": self.pairs,
            "v1_coverage": (float(self.ts1[0]), float(self.ts1[-1])),
            "v2_coverage": (float(self.ts2[0]), float(self.ts2[-1])),
            "v1_interval": float(self.ah1), "v2_interval": float(self.ah2),
            "mode": self.mode, "sync_tracks": (self.track1, self.track2),
            "residual_mean": self.rmean, "residual_max": self.rmax,
            "residual_end": self.rend,
            "coarse_offset": self.coarse_offset,
            "segments": self.segments,
            "warnings": self.decode_warnings,
            "audio_offset": self.audio_offset,
            "audio_speed": self.audio_speed,
            "visual_corrected": vc,
            "visual_offset": vr["offset"] if vc else None,
            "visual_speed": vr["speed"] if vc else None,
            "visual_score": vr["score"] if vc else None,
            "audio_visual_score": vr.get("audio_score") if vc else None,
            "v1_lufs": self.v1_lufs,
            "v2_lufs": self.v2_lufs,
        }
