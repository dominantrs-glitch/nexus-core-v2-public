// Preserved tmKey_ from the production Apps Script captured on 2026-09-24.
// Source: runtime/morning-mail-20260924/gas-transfer/original-code-exact.gs
// Original file SHA-256: 8dfbfa06d41fadda5b43069ef373ba66cf5772f6e8c08a4bbeffddf3236437ff
// Only this non-secret function is retained; line endings normalized to LF.
// This fixture deliberately remains independent of the replacement implementation.
function tmKey_(parts) {
  return 'TM_SEND_'+Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,JSON.stringify(parts),Utilities.Charset.UTF_8)
    .map(function(b){return ('0'+((b+256)%256).toString(16)).slice(-2);}).join('');
}
