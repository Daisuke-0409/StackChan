/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * The upstream file ships weak stubs -- generate_auth_token() returned the
 * literal "hi-stack-chan" -- because the real implementation lives in a
 * closed-source object that is linked into the factory firmware and is not
 * part of this repository. A firmware built from source therefore sends a
 * meaningless Authorization header, M5Stack's relay rejects the handshake,
 * and the Avatar app sits on "Connecting to server..." forever.
 *
 * What M5Stack kept private is the implementation, not the scheme: server
 * README section 8.1 documents it exactly, and server/utility/rsa.go shows
 * the verification side. So this is that scheme, implemented against our own
 * server and our own key pair:
 *
 *     plain  = "<MAC>|<nonce>|<unix seconds>"
 *     cipher = RSA-OAEP(SHA-256 digest, SHA-256 MGF1, no label, server pubkey)
 *     header = Base64(cipher)
 *
 * The server splits on '|' and takes parts[0] and parts[2], so all three
 * fields must be present -- with only two it indexes past the end of the
 * slice (internal/web_socket/web_socket.go guards on len < 2 but reads [2]).
 *
 * The timestamp is checked against server time with a +/-10s window, so the
 * device clock has to be real: Hal::startSntp() must have synchronised before
 * a token is worth generating.
 */
#include "secret_logic.h"
#include <sdkconfig.h>

#include <cstdio>
#include <cstring>
#include <ctime>

#include <esp_mac.h>
#include <esp_random.h>
#include <mbedtls/base64.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/pk.h>
#include <mbedtls/rsa.h>
#include <mooncake_log.h>

namespace secret_logic {

namespace {

constexpr const char* kTag = "secret_logic";

// Public half of the key pair the local server was configured with. A public
// key is not a secret, so it lives in the source; the matching private key is
// only ever in the server's config, which is kept outside this repository.
constexpr const char kServerPublicKeyPem[] =
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAiaExoh8NomaHUP7ScuK/\n"
    "LQh1gZF6/7gmr8Apblwt3NiE6ebtwUUzq63Kf9QJZaWWV1uQ2EmiwTVjfdzfmVcJ\n"
    "mNMlh7n19ZOIv9oTwQapTOpMP8n5DAtM/ipYWrA7IbElOMhggCPtamtdDTWRlxq5\n"
    "hm2myzGmv9Dgn4VvyeEPARQzNLYPpnRvdJ7LR11CWZ5xSWT0eRFUzpJhF8ZS9W8P\n"
    "xB9fv+9/esSCID0ERerYvpgheOEgGCke/ogWQJrHY7tju1DGm2tFPIvOrToLrPyz\n"
    "XZcixENN4m8IcodjxWX9Fw0e+khfBm3nOkr6ksQ0fqCHNdtU7Lcg0F/EmwfJyHQj\n"
    "MQIDAQAB\n"
    "-----END PUBLIC KEY-----\n";

// The server keys its client pool on this string and the app binds a device by
// the same value, so the format has to match what a user would type: upper
// case, colon separated.
std::string StationMacUpper()
{
    uint8_t mac[6] = {};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK) {
        return "";
    }
    char buf[18];
    std::snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3],
                  mac[4], mac[5]);
    return buf;
}

}  // namespace

std::string get_server_url()
{
#ifdef CONFIG_STACKCHAN_SERVER_URL
    return CONFIG_STACKCHAN_SERVER_URL;
#else
    return "http://localhost:3000";
#endif
}

std::string generate_auth_token()
{
    const std::string mac = StationMacUpper();
    if (mac.empty()) {
        mclog::tagError(kTag, "cannot read station MAC; no auth token");
        return "";
    }

    // Unsynchronised clocks read as 1970 here, and the server's +/-10s window
    // would reject that with an error that looks like a key problem. Say so
    // plainly instead of generating a token that cannot possibly work.
    const time_t now = std::time(nullptr);
    if (now < 1700000000) {
        mclog::tagError(kTag, "clock not synchronised (ts={}); SNTP must land first",
                        static_cast<long>(now));
        return "";
    }

    char plain[96];
    const int plain_len =
        std::snprintf(plain, sizeof(plain), "%s|%08lx%08lx|%ld", mac.c_str(),
                      static_cast<unsigned long>(esp_random()),
                      static_cast<unsigned long>(esp_random()), static_cast<long>(now));
    if (plain_len <= 0 || plain_len >= static_cast<int>(sizeof(plain))) {
        mclog::tagError(kTag, "token plaintext did not fit");
        return "";
    }

    mbedtls_pk_context pk;
    mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context ctr_drbg;
    mbedtls_pk_init(&pk);
    mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_init(&ctr_drbg);

    std::string token;
    do {
        static const char kSeed[] = "tachikoma-stackchan-auth";
        if (mbedtls_ctr_drbg_seed(&ctr_drbg, mbedtls_entropy_func, &entropy,
                                  reinterpret_cast<const unsigned char*>(kSeed),
                                  sizeof(kSeed) - 1) != 0) {
            mclog::tagError(kTag, "ctr_drbg seed failed");
            break;
        }
        // The length must include the terminator: mbedtls decides PEM vs DER by
        // looking for the "-----BEGIN" header in a NUL-terminated buffer.
        if (mbedtls_pk_parse_public_key(&pk,
                                        reinterpret_cast<const unsigned char*>(kServerPublicKeyPem),
                                        sizeof(kServerPublicKeyPem)) != 0) {
            mclog::tagError(kTag, "server public key failed to parse");
            break;
        }
        if (!mbedtls_pk_can_do(&pk, MBEDTLS_PK_RSA)) {
            mclog::tagError(kTag, "server key is not RSA");
            break;
        }

        mbedtls_rsa_context* rsa = mbedtls_pk_rsa(pk);
        // PKCS#1 v2.1 is OAEP; the digest here sets both the OAEP hash and the
        // MGF1 hash, which is what crypto/rsa.EncryptOAEP(sha256.New(), ...)
        // does on the server side.
        if (mbedtls_rsa_set_padding(rsa, MBEDTLS_RSA_PKCS_V21, MBEDTLS_MD_SHA256) != 0) {
            mclog::tagError(kTag, "cannot select OAEP-SHA256");
            break;
        }

        const size_t cipher_len = mbedtls_rsa_get_len(rsa);
        unsigned char cipher[512];
        if (cipher_len > sizeof(cipher)) {
            mclog::tagError(kTag, "RSA modulus larger than the output buffer");
            break;
        }
        // Label is empty, matching the nil label the server decrypts with.
        if (mbedtls_rsa_rsaes_oaep_encrypt(rsa, mbedtls_ctr_drbg_random, &ctr_drbg, nullptr, 0,
                                           static_cast<size_t>(plain_len),
                                           reinterpret_cast<const unsigned char*>(plain),
                                           cipher) != 0) {
            mclog::tagError(kTag, "OAEP encrypt failed");
            break;
        }

        unsigned char b64[768];
        size_t b64_len = 0;
        if (mbedtls_base64_encode(b64, sizeof(b64), &b64_len, cipher, cipher_len) != 0) {
            mclog::tagError(kTag, "base64 encode failed");
            break;
        }
        token.assign(reinterpret_cast<char*>(b64), b64_len);
        mclog::tagInfo(kTag, "auth token built for {} ({} chars)", mac.c_str(), token.size());
    } while (false);

    mbedtls_ctr_drbg_free(&ctr_drbg);
    mbedtls_entropy_free(&entropy);
    mbedtls_pk_free(&pk);
    return token;
}

__attribute__((weak)) std::string generate_handshake_token(std::string_view data)
{
    return "hi-stack-chan";
}

}  // namespace secret_logic
