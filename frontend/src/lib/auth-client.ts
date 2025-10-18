
import { createAuthClient } from "better-auth/react";
import { browser } from "$app/environment";

export const authClient = createAuthClient({
    baseURL: browser ? window.location.origin : undefined,
});
