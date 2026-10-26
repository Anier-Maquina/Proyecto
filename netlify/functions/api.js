const BACKEND_ORIGIN = "https://api.serviciosgrupoada.es";
const FUNCTION_PATH = "/.netlify/functions/api";
const HOP_BY_HOP_HEADERS = new Set([
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade"
]);

function getApiPath(event) {
    const requestPath = event.path || new URL(event.rawUrl).pathname;
    if (requestPath === FUNCTION_PATH) {
        return "/api";
    }
    if (requestPath.startsWith(`${FUNCTION_PATH}/`)) {
        return `/api/${requestPath.slice(FUNCTION_PATH.length + 1)}`;
    }
    if (requestPath === "/api" || requestPath.startsWith("/api/")) {
        return requestPath;
    }
    return null;
}

function getSearchParams(event) {
    const searchParams = new URLSearchParams();
    const multiValueParams = event.multiValueQueryStringParameters || {};

    for (const [key, values] of Object.entries(multiValueParams)) {
        for (const value of values || []) {
            searchParams.append(key, value);
        }
    }

    if (Object.keys(multiValueParams).length === 0) {
        for (const [key, value] of Object.entries(event.queryStringParameters || {})) {
            if (value !== null && value !== undefined) {
                searchParams.append(key, value);
            }
        }
    }

    return searchParams;
}

function jsonError(statusCode, message) {
    return {
        statusCode,
        headers: { "content-type": "application/json; charset=utf-8" },
        body: JSON.stringify({ error: message })
    };
}

export async function handler(event) {
    const apiPath = getApiPath(event);
    if (!apiPath) {
        return jsonError(404, "Ruta de API no encontrada.");
    }

    const backendUrl = new URL(apiPath, BACKEND_ORIGIN);
    backendUrl.search = getSearchParams(event).toString();

    const requestHeaders = new Headers();
    for (const [name, value] of Object.entries(event.headers || {})) {
        const normalizedName = name.toLowerCase();
        if (!HOP_BY_HOP_HEADERS.has(normalizedName) && value) {
            requestHeaders.set(name, value);
        }
    }

    const method = (event.httpMethod || "GET").toUpperCase();
    const body = event.body
        ? event.isBase64Encoded
            ? Buffer.from(event.body, "base64")
            : event.body
        : undefined;

    try {
        const upstream = await fetch(backendUrl, {
            method,
            headers: requestHeaders,
            body: method === "GET" || method === "HEAD" ? undefined : body,
            redirect: "manual"
        });
        const responseHeaders = {};
        for (const [name, value] of upstream.headers) {
            if (!HOP_BY_HOP_HEADERS.has(name.toLowerCase())) {
                responseHeaders[name] = value;
            }
        }

        const responseBytes = method === "HEAD" ? null : Buffer.from(await upstream.arrayBuffer());
        const contentType = upstream.headers.get("content-type") || "";
        const isText = /^(text\/|application\/(json|[^;]+\+json|javascript|xml|x-www-form-urlencoded))/.test(contentType);

        return {
            statusCode: upstream.status,
            headers: responseHeaders,
            body: responseBytes
                ? isText
                    ? responseBytes.toString("utf8")
                    : responseBytes.toString("base64")
                : "",
            isBase64Encoded: Boolean(responseBytes && !isText)
        };
    } catch (error) {
        console.error("Error al conectar con el backend de la API:", error);
        return jsonError(502, "No se pudo conectar con el servidor de la API.");
    }
}
