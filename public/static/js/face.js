/* Face helpers — face-api.js (pre-trained models, loaded from CDN).
   The browser extracts a 128-number face descriptor from a camera frame
   or an uploaded photo. Only the descriptor ever leaves the device:
   no photo is uploaded or stored anywhere. */

let modelsLoaded = false;
let modelsLoading = null;

async function loadFaceModels() {
  if (modelsLoaded) return;
  if (!modelsLoading) {
    modelsLoading = (async () => {
      const base = "https://cdn.jsdelivr.net/gh/justadudewhohacks/face-api.js@0.22.2/weights";
      await faceapi.nets.tinyFaceDetector.loadFromUri(base);
      await faceapi.nets.faceLandmark68Net.loadFromUri(base);
      await faceapi.nets.faceRecognitionNet.loadFromUri(base);
      modelsLoaded = true;
    })();
  }
  return modelsLoading;
}

async function descriptorFromElement(el) {
  const detection = await faceapi
    .detectSingleFace(el, new faceapi.TinyFaceDetectorOptions({ scoreThreshold: 0.4 }))
    .withFaceLandmarks()
    .withFaceDescriptor();
  if (!detection) return null;
  return Array.from(detection.descriptor);
}

async function startCamera(videoEl) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
    audio: false,
  });
  videoEl.srcObject = stream;
  await videoEl.play();
  return stream;
}

function stopStream(stream) {
  if (stream) stream.getTracks().forEach((t) => t.stop());
}

/* Capture the current video frame to a canvas, then run detection on it.
   Detection on a still frame is more reliable than on a live stream. */
async function descriptorFromVideo(videoEl) {
  const canvas = document.createElement("canvas");
  canvas.width = videoEl.videoWidth;
  canvas.height = videoEl.videoHeight;
  canvas.getContext("2d").drawImage(videoEl, 0, 0);
  return descriptorFromElement(canvas);
}

function descriptorFromFile(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = async () => {
      try {
        resolve(await descriptorFromElement(img));
      } catch (e) {
        reject(e);
      }
    };
    img.onerror = () => reject(new Error("could not read that image"));
    img.src = URL.createObjectURL(file);
  });
}

async function getPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("location is not available in this browser"));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracy: pos.coords.accuracy,
      }),
      (err) => reject(new Error(
        err.code === 1
          ? "location permission denied — allow it and try again"
          : "could not get your location — try again outside or near a window"
      )),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }
    );
  });
}
