import type { AxiosInstance } from 'axios';
import { useLocalState } from '../../states/LocalState';
import { useUserState } from '../../states/UserState';
import { LocalPrintClient } from './LocalPrintClient';

let client: Promise<LocalPrintClient> | undefined;
let sessionEpoch = 0;

useUserState.subscribe((state, previous) => {
  if (
    state.user?.pk !== previous.user?.pk ||
    state.is_authed !== previous.is_authed
  ) {
    sessionEpoch++;
    client?.then((value) => value.disconnect()).catch(() => undefined);
  }
});

export function localPrintSession() {
  const state = useUserState.getState();
  return state.isLoggedIn()
    ? `${useLocalState.getState().getHost()}:${state.user?.pk}:${sessionEpoch}`
    : undefined;
}

export function getLocalPrintClient(api: AxiosInstance) {
  client ??= import('qz-tray').then(
    ({ default: qz }) => new LocalPrintClient(qz, api, localPrintSession)
  );
  return client;
}
