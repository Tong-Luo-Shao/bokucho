// font_faces.js -- the faces the image editor shows a text layer in.
//
// A layer names its face by a file, or by `bundled:<file>` for one Bokucho
// ships, and neither is a family the webview knows: the face is read from the
// file typesetting will use, so the preview is lettered as the picture will be.

// One load per face for the session: some of the shipped faces are tens of
// megabytes, and every editor opened after the first reuses them.
const faces = new Map();

function bytesOf(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

// The CSS family the face `ref` is loaded under, or null when `ref` names no
// font file -- an older layer may still name a family, which is left to CSS.
export function loadFace(bridge, ref) {
  if (!faces.has(ref)) {
    const family = `bokucho-face-${faces.size}`;
    faces.set(
      ref,
      (async () => {
        try {
          const { data } = await bridge.typeset_font_data(ref);
          const face = await new FontFace(family, bytesOf(data)).load();
          document.fonts.add(face);
          return family;
        } catch {
          return null;
        }
      })(),
    );
  }
  return faces.get(ref);
}
