# Postseason button navigation issue

When the "플레이 오프 진행" button is clicked, `startPostseasonFlow` runs and only switches the screen after it finishes loading postseason state and stats. The state refresh has no error handling, so if `/api/postseason/state` fails (for example, a 500 or invalid JSON), the promise rejects before `showScreen('playoff')` executes. The UI stays on the main screen with no fallback. The setup/reset calls are wrapped in a try/catch, but the subsequent `refreshPostseasonState()` call is not, so any failure there blocks navigation without user feedback.
