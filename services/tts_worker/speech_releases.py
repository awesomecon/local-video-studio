"""Pinned, public model/runtime identifiers; no model-library imports."""
MING_REVISION = "9332fb9443325d4726ce02cefd4ec1c211df4079"
DRAMA_REVISION = "97b414614dcd0338fe70dbb0af31a7738f126d2b"
DRAMA_ASSET = "drama-rs-v0.3.0-x86_64-unknown-linux-gnu.tar.gz"
DRAMA_SHA256 = "7cc78e12bcb4feebabb3a4f42e2642e5189e7803ccf26427c51e433652769de1"
RELEASES = {
    "fish_drama_3": ("apiplant/drama-q4", "1fce9bd21d46a0311294aebfad19f994df90902f", 22_938_419_178),
    "ming_tts_tiny": ("inclusionAI/Ming-3.0-tts-tiny", "4715ef5a2f02901f75687d538b6920fd0dd11e6b", 18_006_963_962),
    "ming_tts_05b": ("inclusionAI/Ming-3.0-tts-0.5B", "7247f4c94a5b2e987404ed28a5cfec41b1a4ca61", 3_004_401_795),
}
MODEL_SUBDIRS = {
    "fish_drama_3": "models/tts/fish/drama-q4",
    "ming_tts_tiny": "models/tts/ming/Ming-3.0-tts-tiny",
    "ming_tts_05b": "models/tts/ming/Ming-3.0-tts-0.5B",
}
