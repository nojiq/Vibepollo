#pragma once

#include "remote_session.h"

#include <array>
#include <atomic>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace rtsp_stream::pending_policy {
  constexpr int MAX_CAPTURE_FRAMERATE = 4000;

  struct normalized_framerate_t {
    int capture_framerate;
    int encoding_framerate;
    friend bool operator==(const normalized_framerate_t &, const normalized_framerate_t &) = default;
  };

  std::optional<normalized_framerate_t> normalize_requested_framerate(std::int64_t requested_framerate);
  std::optional<normalized_framerate_t> parse_requested_framerate(std::string_view requested_framerate);

  enum class initial_route_e { reject, plaintext, encrypted };

  struct pending_owner_t {
    remote_session::role_e role {remote_session::role_e::game};
    std::string client_uuid;
    std::uint64_t generation {};
  };

  // Selects an unbound transport route from the first four wire bytes.  This
  // small policy seam is used by rtsp.cpp so NAT-mixed plaintext/encrypted
  // routing has direct component coverage.
  initial_route_e choose_initial_route(bool plaintext_available, bool encrypted_available, const std::array<std::uint8_t, 4> &first_word);
  bool game_session_requires_shutdown(bool game_runtime_active, remote_session::role_e role);
  bool control_server_should_remain_alive(bool game_runtime_active, bool has_processless_live_session, bool has_game_session_pending_or_draining);
  bool teardown_cleanup_allowed(std::uint32_t active_teardown_sessions, std::uint32_t pending_teardown_sessions);
  bool disconnect_scope_matches(remote_session::role_e candidate_role, remote_session::role_e requested_role, bool client_matches, bool all_clients);
  std::vector<pending_owner_t> expired_remote_input_owners(const std::vector<pending_owner_t> &expired);
  std::vector<pending_owner_t> disconnect_input_owners_to_forget(const std::vector<pending_owner_t> &removed);

  // Keeps a session visible as teardown work between RTSP registry removal and
  // stream::session::join() incrementing its own completed-teardown counter.
  class teardown_reservation_t {
  public:
    explicit teardown_reservation_t(std::atomic_uint &counter) noexcept:
        counter_(&counter) {
      counter_->fetch_add(1, std::memory_order_acq_rel);
    }

    teardown_reservation_t(const teardown_reservation_t &) = delete;
    teardown_reservation_t &operator=(const teardown_reservation_t &) = delete;

    teardown_reservation_t(teardown_reservation_t &&other) noexcept:
        counter_(std::exchange(other.counter_, nullptr)) {}

    teardown_reservation_t &operator=(teardown_reservation_t &&other) noexcept {
      if (this != &other) {
        release();
        counter_ = std::exchange(other.counter_, nullptr);
      }
      return *this;
    }

    ~teardown_reservation_t() { release(); }

    void release() noexcept {
      if (counter_) {
        counter_->fetch_sub(1, std::memory_order_acq_rel);
        counter_ = nullptr;
      }
    }

  private:
    std::atomic_uint *counter_;
  };
}  // namespace rtsp_stream::pending_policy
