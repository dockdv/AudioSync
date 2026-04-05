#!/usr/bin/env python3

from dataclasses import dataclass, field


@dataclass
class SessionContext:
    # --- paths ---
    v1_path: str = None
    v2_path: str = None
    out_path: str = None

    # --- probe data ---
    v1_info: dict = None
    v2_info: dict = None
    v1_duration: float = 0

    # --- alignment input ---
    align_track1: int = 0
    align_track2: int = 0
    vocal_filter: bool = False
    measure_lufs: bool = False

    # --- alignment internals (used by sync_engine, freed after align) ---
    align_dur1: float = 0
    align_dur2: float = 0
    align_hop1: float = 0
    align_hop2: float = 0
    align_max_samples: int = 0
    v1_has_video: bool = False
    v2_has_video: bool = False
    audio1: object = None
    audio2: object = None
    ts1: object = None
    ts2: object = None
    fp1_main: object = None
    fp2_main: object = None
    ah1: object = None
    ah2: object = None
    decode_warnings: list = field(default_factory=list)
    coarse_offset: float = 0.0
    xcorr_speed: float = 1.0
    audio_offset: float = 0.0
    audio_speed: float = 1.0
    alt_offsets: list = field(default_factory=list)
    ds1_seg: object = None
    ds2_seg: object = None
    ds_rate: float = 0.0
    visual_refined_offset: float = None
    v2_start_delay: float = 0.0
    align_mode: str = ""
    align_a: float = 1.0
    align_b: float = 0.0
    align_ni: int = 0
    align_total_good: int = 0
    align_pairs: list = field(default_factory=list)
    align_rmean: float = 0.0
    align_rmax: float = 0.0
    align_rend: float = 0.0

    # --- alignment result (shared with merge) ---
    atempo: float = 1.0
    offset: float = 0.0
    segments: list = None
    v1_lufs: float = None
    v2_lufs: float = None

    # --- merge input ---
    v1_stream_indices: list = None
    v2_stream_indices: list = None
    audio_metadata: list = None
    audio_order: list = None
    default_audio_index: int = None
    v1_sub_metadata: list = None
    v2_sub_metadata: list = None
    gain_match: bool = False
    v1_has_attachments: bool = True
    v2_has_attachments: bool = False

    # --- merge derived (set by prepare_merge / set_v2_mode) ---
    is_remux: bool = False
    ffmpeg_path: str = None
    v1_sample_rate: int = 48000
    v1_dur: float = 0

    # v1 classified stream indices
    v1_stream_types: dict = field(default_factory=dict)
    v1_vid_si: list = field(default_factory=list)
    v1_aud_si: list = field(default_factory=list)
    v1_sub_si: list = field(default_factory=list)
    v1_other_si: list = field(default_factory=list)
    v1_has_subs: bool = False

    # v2 classified stream indices
    v2_stream_types: dict = field(default_factory=dict)
    v2_aud_si: list = field(default_factory=list)
    v2_sub_si: list = field(default_factory=list)
    v2_aud_indices: list = field(default_factory=list)

    # v1 mkvmerge track ids
    v1_vid_tids: list = field(default_factory=list)
    v1_aud_tids: list = field(default_factory=list)
    v1_sub_tids: list = field(default_factory=list)
    v1_other_tids: list = field(default_factory=list)

    # v2 mode and mkvmerge track ids
    tmp_audio_path: str = None
    v2_streamcopy: bool = False
    v2_aud_tids: list = field(default_factory=list)
    v2_sub_tids: list = field(default_factory=list)

    # audio ordering (file_id, tid) tuples
    audio_ft: list = field(default_factory=list)
    audio_ft_ordered: list = field(default_factory=list)
    default_audio_ft: tuple = None
    audio_src_to_meta: dict = field(default_factory=dict)

    # --- runtime (set per-task, not persistent) ---
    progress_cb: object = None
    cancel: object = None
