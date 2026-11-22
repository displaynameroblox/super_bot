package io.superbot.minecraft;
import com.google.gson.*;
import java.io.IOException;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.nio.file.*;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import javax.net.ssl.*;
public final class HttpsTransport implements Transport {
    private static final String DESCRIPTOR = "/.well-known/superbot";
    private static final String SBP_PATH = "/sbp/v0.1";
    private static final class HttpStatus extends IOException {
        final int status;
        HttpStatus(int status) {
            super("SBP HTTP status " + status);
            this.status = status;
        }
    }
    private final URI origin;
    private final HttpClient client;
    private boolean persistentPrivateChat;
    public HttpsTransport(String url, Path ca) throws Exception {
        origin = URI.create(url);
        if (!"https".equals(origin.getScheme()) || origin.getHost()==null || origin.getUserInfo()!=null || origin.getQuery()!=null || origin.getFragment()!=null || !(origin.getPath().isEmpty() || origin.getPath().equals("/"))) throw new IllegalArgumentException("Expected HTTPS origin");
        var builder = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).followRedirects(HttpClient.Redirect.NEVER);
        if (ca != null) {
            var store = KeyStore.getInstance(KeyStore.getDefaultType()); store.load(null);
            try (var in=Files.newInputStream(ca)) { int n=0; for(var cert: CertificateFactory.getInstance("X.509").generateCertificates(in)) store.setCertificateEntry("ca"+n++, cert); }
            var managers=TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()); managers.init(store);
            var context=SSLContext.getInstance("TLS"); context.init(null,managers.getTrustManagers(),null); builder.sslContext(context);
        }
        client=builder.build();
    }
    /** Verify the configured HTTPS endpoint before sending the adapter credential. */
    public boolean verifyHost(String expectedNodeId) throws Exception {
        persistentPrivateChat = false;
        try {
            var node = exchange("GET", DESCRIPTOR, null, null).getAsJsonObject();
            String id = node.get("node_id").getAsString();
            if (!id.matches("nod_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
                || (expectedNodeId != null && !expectedNodeId.equals(id))) {
                throw new IOException("SBP node identity mismatch");
            }
            if (!"ready".equals(node.get("status").getAsString())
                || !SBP_PATH.equals(node.get("sbp_path").getAsString())
                || !node.getAsJsonArray("sbp_versions").contains(new JsonPrimitive("0.1"))) {
                throw new IOException("SBP node is not ready for protocol 0.1");
            }
            persistentPrivateChat = node.has("features")
                && node.getAsJsonArray("features").contains(new JsonPrimitive("minecraft.private_chat"));
            return true;
        } catch (HttpStatus e) {
            if (e.status != 404 || expectedNodeId != null) throw e;
            var protocol = exchange("GET", SBP_PATH + "/protocol", null, null).getAsJsonObject();
            if (!protocol.getAsJsonArray("versions").contains(new JsonPrimitive("0.1"))) {
                throw new IOException("SBP host does not support protocol 0.1");
            }
            return false;
        } catch (RuntimeException e) {
            throw new IOException("Invalid SBP node descriptor", e);
        }
    }

    public boolean supportsPersistentPrivateChat() {
        return persistentPrivateChat;
    }

    public JsonElement request(String method, String path, String token, JsonObject body) throws Exception {
        if (!path.startsWith(SBP_PATH + "/") || path.contains("..") || path.contains("://")) throw new IllegalArgumentException("Invalid SBP path");
        return exchange(method, path, token, body);
    }

    private JsonElement exchange(String method, String path, String token, JsonObject body) throws Exception {
        var builder=HttpRequest.newBuilder(origin.resolve(path)).timeout(Duration.ofSeconds(path.endsWith("/tasks") || path.endsWith("/private-chat") ? 180 : 20)).header("Content-Type","application/json");
        if (token != null) builder.header("Authorization", "Bearer " + token);
        var request=builder
            .method(method,body==null?HttpRequest.BodyPublishers.noBody():HttpRequest.BodyPublishers.ofString(body.toString())).build();
        var response=client.send(request,HttpResponse.BodyHandlers.ofInputStream());
        byte[] bytes; try(var in=response.body()) { bytes=in.readNBytes(1048577); }
        if(bytes.length>1048576) throw new java.io.IOException("SBP response too large");
        if(response.statusCode()/100!=2) throw new HttpStatus(response.statusCode());
        return JsonParser.parseString(new String(bytes, java.nio.charset.StandardCharsets.UTF_8));
    }
}
