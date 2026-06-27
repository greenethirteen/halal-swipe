// Firebase (analytics only). Auth is handled server-side for now.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import { getAnalytics } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-analytics.js";

const firebaseConfig = {
  apiKey: "AIzaSyAMWd16OmvSJtMAYOuijHnCJQZ1IaSJrNQ",
  authDomain: "halalswipe.firebaseapp.com",
  projectId: "halalswipe",
  storageBucket: "halalswipe.firebasestorage.app",
  messagingSenderId: "924628982832",
  appId: "1:924628982832:web:28b573ca44b3229db121f5",
  measurementId: "G-025YQVM4N2"
};

try {
  const app = initializeApp(firebaseConfig);
  getAnalytics(app);
} catch (e) {
  // Analytics is non-critical; ignore failures (e.g. offline).
}
