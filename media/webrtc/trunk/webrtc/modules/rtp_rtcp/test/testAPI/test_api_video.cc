/*
 *  Copyright (c) 2012 The WebRTC project authors. All Rights Reserved.
 *
 *  Use of this source code is governed by a BSD-style license
 *  that can be found in the LICENSE file in the root of the source
 *  tree. An additional intellectual property rights grant can be found
 *  in the file PATENTS.  All contributing project authors may
 *  be found in the AUTHORS file in the root of the source tree.
 */

#include <stdlib.h>

#include <algorithm>
#include <vector>

#include "gtest/gtest.h"
#include "webrtc/common_types.h"
#include "webrtc/modules/rtp_rtcp/interface/rtp_rtcp.h"
#include "webrtc/modules/rtp_rtcp/interface/rtp_rtcp_defines.h"
#include "webrtc/modules/rtp_rtcp/source/rtp_utility.h"
#include "webrtc/modules/rtp_rtcp/test/testAPI/test_api.h"

namespace webrtc {

class RtpRtcpVideoTest : public ::testing::Test {
 protected:
  RtpRtcpVideoTest()
      : test_id_(123),
        test_ssrc_(3456),
        test_timestamp_(4567),
        test_sequence_number_(2345) {
  }
  ~RtpRtcpVideoTest() {}

  virtual void SetUp() {
    transport_ = new LoopBackTransport();
    receiver_ = new RtpReceiver();
    RtpRtcp::Configuration configuration;
    configuration.id = test_id_;
    configuration.audio = false;
    configuration.clock = &fake_clock;
    configuration.incoming_data = receiver_;
    configuration.outgoing_transport = transport_;

    video_module_ = RtpRtcp::CreateRtpRtcp(configuration);

    EXPECT_EQ(0, video_module_->SetRTCPStatus(kRtcpCompound));
    EXPECT_EQ(0, video_module_->SetSSRC(test_ssrc_));
    EXPECT_EQ(0, video_module_->SetNACKStatus(kNackRtcp));
    EXPECT_EQ(0, video_module_->SetStorePacketsStatus(true));
    EXPECT_EQ(0, video_module_->SetSendingStatus(true));

    transport_->SetSendModule(video_module_);

    VideoCodec video_codec;
    memset(&video_codec, 0, sizeof(video_codec));
    video_codec.plType = 123;
    memcpy(video_codec.plName, "I420", 5);

    EXPECT_EQ(0, video_module_->RegisterSendPayload(video_codec));
    EXPECT_EQ(0, video_module_->RegisterReceivePayload(video_codec));

    payload_data_length_ = sizeof(video_frame_);

    for (int n = 0; n < payload_data_length_; n++) {
      video_frame_[n] = n%10;
    }
  }

  WebRtc_Word32 BuildRTPheader(WebRtc_UWord8* dataBuffer,
                               WebRtc_UWord32 timestamp,
                               WebRtc_UWord32 sequence_number) {
    dataBuffer[0] = static_cast<WebRtc_UWord8>(0x80);  // version 2
    dataBuffer[1] = static_cast<WebRtc_UWord8>(kPayloadType);
    ModuleRTPUtility::AssignUWord16ToBuffer(dataBuffer + 2,
                                                    sequence_number);
    ModuleRTPUtility::AssignUWord32ToBuffer(dataBuffer + 4, timestamp);
    ModuleRTPUtility::AssignUWord32ToBuffer(dataBuffer + 8,
                                                    0x1234);  // SSRC.
    WebRtc_Word32 rtpHeaderLength = 12;
    return rtpHeaderLength;
  }

  int PaddingPacket(uint8_t* buffer,
                    WebRtc_UWord32 timestamp,
                    WebRtc_UWord32 sequence_number,
                    WebRtc_Word32 bytes) {
    // Max in the RFC 3550 is 255 bytes, we limit it to be modulus 32 for SRTP.
    int max_length = 224;

    int padding_bytes_in_packet = max_length;
    if (bytes < max_length) {
      padding_bytes_in_packet = (bytes + 16) & 0xffe0;  // Keep our modulus 32.
    }
    // Correct seq num, timestamp and payload type.
    int header_length = BuildRTPheader(buffer, timestamp,
                                       sequence_number);
    buffer[0] |= 0x20;  // Set padding bit.
    WebRtc_Word32* data =
        reinterpret_cast<WebRtc_Word32*>(&(buffer[header_length]));

    // Fill data buffer with random data.
    for (int j = 0; j < (padding_bytes_in_packet >> 2); j++) {
      data[j] = rand();  // NOLINT
    }
    // Set number of padding bytes in the last byte of the packet.
    buffer[header_length + padding_bytes_in_packet - 1] =
        padding_bytes_in_packet;
    return padding_bytes_in_packet + header_length;
  }

  virtual void TearDown() {
    delete video_module_;
    delete transport_;
    delete receiver_;
  }

  int test_id_;
  RtpRtcp* video_module_;
  LoopBackTransport* transport_;
  RtpReceiver* receiver_;
  WebRtc_UWord32 test_ssrc_;
  WebRtc_UWord32 test_timestamp_;
  WebRtc_UWord16 test_sequence_number_;
  WebRtc_UWord8  video_frame_[65000];
  int payload_data_length_;
  FakeRtpRtcpClock fake_clock;
  enum { kPayloadType = 100 };
};

TEST_F(RtpRtcpVideoTest, BasicVideo) {
  WebRtc_UWord32 timestamp = 3000;
  EXPECT_EQ(0, video_module_->SendOutgoingData(kVideoFrameDelta, 123,
                                               timestamp,
                                               timestamp / 90,
                                               video_frame_,
                                               payload_data_length_));
}

TEST_F(RtpRtcpVideoTest, PaddingOnlyFrames) {
  const int kPadSize = 255;
  uint8_t padding_packet[kPadSize];
  uint32_t seq_num = 0;
  uint32_t timestamp = 3000;
  VideoCodec codec;
  codec.codecType = kVideoCodecVP8;
  codec.plType = kPayloadType;
  strncpy(codec.plName, "VP8", 4);
  EXPECT_EQ(0, video_module_->RegisterReceivePayload(codec));
  for (int frame_idx = 0; frame_idx < 10; ++frame_idx) {
    for (int packet_idx = 0; packet_idx < 5; ++packet_idx) {
      int packet_size = PaddingPacket(padding_packet, timestamp, seq_num,
                                      kPadSize);
      ++seq_num;
      EXPECT_EQ(0, video_module_->IncomingPacket(padding_packet, packet_size));
      EXPECT_EQ(0, receiver_->payload_size());
      EXPECT_EQ(packet_size - 12, receiver_->rtp_header().header.paddingLength);
    }
    timestamp += 3000;
    fake_clock.IncrementTime(33);
  }
}

}  // namespace webrtc
