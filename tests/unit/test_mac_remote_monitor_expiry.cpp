#include <gtest/gtest.h>

#include <chrono>
#include <string>

#include "src/platform/macos/remote_monitor_expiry.h"

namespace {
  using expiry_t = platf::macos_virtual_display::remote_monitor_expiry_t;
  using namespace std::chrono_literals;

  const auto start = expiry_t::clock::time_point {};
}

TEST(MacRemoteMonitorExpiry, KeepsPeerGracePeriodsIndependent) {
  expiry_t expiry;
  expiry.transport_lost("surface", 11, start);
  expiry.transport_lost("msi", 22, start + 1s);

  EXPECT_TRUE(expiry.expired(start + 29s).empty());

  const auto first = expiry.expired(start + 30s);
  ASSERT_EQ(first.size(), 1u);
  EXPECT_EQ(first[0], (expiry_t::expired_entry_t {"surface", 11}));

  const auto second = expiry.expired(start + 31s);
  ASSERT_EQ(second.size(), 1u);
  EXPECT_EQ(second[0], (expiry_t::expired_entry_t {"msi", 22}));
  EXPECT_TRUE(expiry.expired(start + 1min).empty());
}

TEST(MacRemoteMonitorExpiry, ResumeCancelsPendingDisconnect) {
  expiry_t expiry;
  expiry.transport_lost("surface", 11, start);
  expiry.resumed("surface");

  EXPECT_TRUE(expiry.expired(start + 30s).empty());
}

TEST(MacRemoteMonitorExpiry, ReconnectGenerationReplacesStaleTimer) {
  expiry_t expiry;
  expiry.transport_lost("surface", 11, start);
  expiry.transport_lost("surface", 12, start + 1s);

  // The old generation's deadline has passed, but its record was replaced.
  EXPECT_TRUE(expiry.expired(start + 30s).empty());

  const auto due = expiry.expired(start + 31s);
  ASSERT_EQ(due.size(), 1u);
  EXPECT_EQ(due[0], (expiry_t::expired_entry_t {"surface", 12}));
}

TEST(MacRemoteMonitorExpiry, ForgetRemovesUnpairedClientTimer) {
  expiry_t expiry;
  expiry.transport_lost("surface", 11, start);
  expiry.forget("surface");

  EXPECT_TRUE(expiry.expired(start + 30s).empty());
}

TEST(MacRemoteMonitorExpiry, OlderNotificationCannotReplaceNewerTimer) {
  expiry_t expiry;
  expiry.transport_lost("surface", 12, start);
  expiry.transport_lost("surface", 11, start + 1s);
  const auto due = expiry.expired(start + 30s);
  ASSERT_EQ(due.size(), 1u);
  EXPECT_EQ(due[0], (expiry_t::expired_entry_t {"surface", 12}));
}

TEST(MacRemoteMonitorExpiry, OlderReleaseCannotCancelNewerTimer) {
  expiry_t expiry;
  expiry.transport_lost("surface", 12, start);
  expiry.forget("surface", 11);
  const auto due = expiry.expired(start + 30s);
  ASSERT_EQ(due.size(), 1u);
  EXPECT_EQ(due[0], (expiry_t::expired_entry_t {"surface", 12}));
}
