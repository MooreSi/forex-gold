"""Static asset paths and the JavaScript the page injects.

Shared because the header needs them too, and a module cannot import them
back out of frontend/app/__init__.py without a cycle. The two audio blobs
stay together: the comment above them explains why one depends on the other,
and splitting them would strand it.
"""
from backend.src.utils.os_utils import repo_root as _repo_root

# Resolved from the repo marker, not by counting parents off __file__ -- this
# package was frontend/app.py until it was split, and a fixed .parent then
# pointed at frontend/app/static, which does not exist.
STATIC_DIR = _repo_root() / "frontend" / "static"

# ── Cash register sound — Web Audio API synthesis ────────────────────────────
# Played in the browser when a trade closes with a profit.
#
# IMPORTANT — browser autoplay policy:
#   AudioContext starts in 'suspended' state unless the user has interacted with
#   the page.  _VC_AUDIO_UNLOCK_JS must be injected once on page load; it attaches
#   click/keydown/touchstart listeners that create and resume the shared context.
#   _CASH_REGISTER_JS then reuses that already-running context.
#
_VC_AUDIO_UNLOCK_JS = """
<script>
(function() {
    window._vcAudioCtx = null;
    function _vcUnlock() {
        if (!window._vcAudioCtx) {
            try { window._vcAudioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
            catch(e) { return; }
        }
        if (window._vcAudioCtx.state === 'suspended') {
            window._vcAudioCtx.resume().catch(function(){});
        }
    }
    ['click','keydown','touchstart'].forEach(function(ev) {
        document.addEventListener(ev, _vcUnlock, {passive: true});
    });
})();
</script>
"""

_CASH_REGISTER_JS = """
(function() {
    try {
        var ctx = window._vcAudioCtx;
        if (!ctx || ctx.state !== 'running') return;
        var t = ctx.currentTime;

        // Mechanical click — very short filtered noise burst
        var sr  = ctx.sampleRate;
        var len = Math.floor(sr * 0.025);
        var buf = ctx.createBuffer(1, len, sr);
        var d   = buf.getChannelData(0);
        for (var i = 0; i < len; i++) {
            d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 2);
        }
        var click     = ctx.createBufferSource();
        var clickGain = ctx.createGain();
        click.buffer  = buf;
        clickGain.gain.setValueAtTime(0.5, t);
        click.connect(clickGain);
        clickGain.connect(ctx.destination);
        click.start(t);

        // Metallic "ching" ring — three harmonics, exponential decay
        [[659, 0.35, 0.7], [1319, 0.22, 0.45], [1976, 0.12, 0.3]].forEach(function(h) {
            var osc  = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type            = 'sine';
            osc.frequency.value = h[0];
            gain.gain.setValueAtTime(h[1], t + 0.008);
            gain.gain.exponentialRampToValueAtTime(0.0001, t + h[2]);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(t + 0.008);
            osc.stop(t + h[2]);
        });
    } catch(e) {}
})();
"""
