// Ambient types for the Google Identity Services global loaded at runtime
// (https://accounts.google.com/gsi/client). We only use the `useIdToken`-style
// `google.accounts.id` surface for the "Continue with Google" button.

interface GoogleCredentialResponse {
  credential: string;
  select_by?: string;
  client_id?: string;
}

interface GooglePromptNotification {
  isNotDisplayed?: boolean;
  isSkipped?: boolean;
  skipped_reason?: string;
  not_displayed_reason?: string;
}

interface GoogleAccountsId {
  initialize: (config: {
    client_id: string;
    callback: (response: GoogleCredentialResponse) => void;
    auto_select?: boolean;
    cancel_on_tap_outside?: boolean;
    itp_support?: boolean;
  }) => void;
  prompt?: (
    callback?: (notification: GooglePromptNotification) => void,
  ) => void;
  renderButton?: (
    parent: HTMLElement,
    options: Record<string, unknown>,
  ) => void;
}

interface GoogleAccounts {
  id: GoogleAccountsId;
}

interface GoogleNamespace {
  accounts?: GoogleAccounts;
}

declare global {
  type GoogleIdService = GoogleAccountsId;

  interface Window {
    google?: GoogleNamespace;
  }
}

export {};
