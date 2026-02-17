#include <Spinnaker.h>
#include <SpinGenApi/SpinnakerGenApi.h>
#include <opencv2/opencv.hpp>

#include <iostream>
#include <vector>
#include <string>
#include <sstream>
#include <iomanip>
#include <filesystem>
#include <atomic>
#include <csignal>
#include <chrono>
#include <thread>
#include <algorithm>

using namespace Spinnaker;
using namespace Spinnaker::GenApi;
namespace fs = std::filesystem;

/* ================= GLOBALS ================= */

std::atomic<bool> running(true);

void signal_handler(int)
{
    running = false;
}

/* ================= ARG PARSER ================= */

struct Args {
    std::vector<std::string> serials;
    std::string out_dir;
    double fps = 1.0;
    double exposure = 5000.0;
    int64_t throughput = 0;
};

Args parse_args(int argc, char** argv)
{
    Args a;
    for (int i = 1; i < argc; i++) {
        std::string k = argv[i];

        if (k == "--cams" && i + 1 < argc) {
            std::stringstream ss(argv[++i]);
            std::string s;
            while (std::getline(ss, s, ',')) {
                a.serials.push_back(s);
            }
        }
        else if (k == "--out" && i + 1 < argc) {
            a.out_dir = argv[++i];
        }
        else if (k == "--fps" && i + 1 < argc) {
            a.fps = std::stod(argv[++i]);
        }
        else if (k == "--exposure" && i + 1 < argc) {
            a.exposure = std::stod(argv[++i]);
        }
        else if (k == "--throughput" && i + 1 < argc) {
            a.throughput = std::stoll(argv[++i]);
        }
    }
    return a;
}

/* ================= TIMESTAMP ================= */

std::string timestamp()
{
    using namespace std::chrono;

    auto now = system_clock::now();
    auto t = system_clock::to_time_t(now);
    std::tm tm{};
    localtime_r(&t, &tm);

    auto ms = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;

    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y%m%d_%H%M%S")
        << "_" << std::setw(3) << std::setfill('0') << ms.count();
    return oss.str();
}

/* ================= CAMERA CONFIG ================= */

bool configure_camera(CameraPtr cam, const Args& args, std::string& serial)
{
    try {
        cam->Init();

        /* -------- SERIAL -------- */
        INodeMap& tlMap = cam->GetTLDeviceNodeMap();
        CStringPtr snNode = CStringPtr(tlMap.GetNode("DeviceSerialNumber"));
        if (!IsAvailable(snNode) || !IsReadable(snNode)) {
            throw std::runtime_error("DeviceSerialNumber not readable");
        }
        serial = snNode->GetValue().c_str();

        INodeMap& nm = cam->GetNodeMap();
        INodeMap& sm = cam->GetTLStreamNodeMap();

        /* -------- ACQUISITION MODE (CRITICAL) -------- */
        CEnumerationPtr acqMode = nm.GetNode("AcquisitionMode");
        CEnumEntryPtr cont = acqMode->GetEntryByName("Continuous");
        acqMode->SetIntValue(cont->GetValue());

        /* -------- TRIGGER OFF -------- */
        CEnumerationPtr trig = nm.GetNode("TriggerMode");
        CEnumEntryPtr trigOff = trig->GetEntryByName("Off");
        trig->SetIntValue(trigOff->GetValue());

        /* -------- EXPOSURE -------- */
        CEnumerationPtr expAuto = nm.GetNode("ExposureAuto");
        CEnumEntryPtr expOff = expAuto->GetEntryByName("Off");
        expAuto->SetIntValue(expOff->GetValue());

        CFloatPtr exp = nm.GetNode("ExposureTime");
        exp->SetValue(std::min(std::max(args.exposure, exp->GetMin()), exp->GetMax()));

        /* -------- FPS -------- */
        CBooleanPtr fpsEnable = nm.GetNode("AcquisitionFrameRateEnable");
        fpsEnable->SetValue(true);

        CFloatPtr fps = nm.GetNode("AcquisitionFrameRate");
        fps->SetValue(std::min(std::max(args.fps, fps->GetMin()), fps->GetMax()));

        /* -------- THROUGHPUT -------- */
        if (args.throughput > 0) {
            CIntegerPtr tp = nm.GetNode("DeviceLinkThroughputLimit");
            tp->SetValue(std::min(args.throughput, tp->GetMax()));
        }

        /* ======== STREAM CONFIG (CRITICAL) ======== */

        CEnumerationPtr bufMode = sm.GetNode("StreamBufferCountMode");
        CEnumEntryPtr manual = bufMode->GetEntryByName("Manual");
        bufMode->SetIntValue(manual->GetValue());

        CIntegerPtr bufCount = sm.GetNode("StreamBufferCountManual");
        bufCount->SetValue(16);

        CEnumerationPtr handling = sm.GetNode("StreamBufferHandlingMode");
        CEnumEntryPtr oldest = handling->GetEntryByName("OldestFirst");
        handling->SetIntValue(oldest->GetValue());

        /* ======================================== */

        cam->BeginAcquisition();

        /* IMPORTANT: allow stream to stabilize */
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        return true;
    }
    catch (const std::exception& e) {
        std::cerr << "[ERROR] Configure failed: " << e.what() << "\n";
        return false;
    }
}

/* ================= MAIN ================= */

int main(int argc, char** argv)
{
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    Args args = parse_args(argc, argv);

    if (args.serials.empty() || args.out_dir.empty()) {
        std::cerr << "Usage:\n"
                  << "  --cams s1,s2 --out dir [--fps N] [--exposure us] [--throughput bps]\n";
        return 1;
    }

    SystemPtr system = System::GetInstance();
    CameraList camList = system->GetCameras();

    std::vector<CameraPtr> cams;
    std::vector<std::string> sns;

    /* -------- SELECT CAMERAS -------- */
    for (unsigned i = 0; i < camList.GetSize(); i++) {
        CameraPtr cam = camList.GetByIndex(i);

        INodeMap& tlMap = cam->GetTLDeviceNodeMap();
        CStringPtr snNode = CStringPtr(tlMap.GetNode("DeviceSerialNumber"));

        if (!IsAvailable(snNode) || !IsReadable(snNode)) {
            continue;
        }

        std::string sn = snNode->GetValue().c_str();

        if (std::find(args.serials.begin(), args.serials.end(), sn)
            != args.serials.end()) {
            cams.push_back(cam);
        }
    }

    if (cams.empty()) {
        std::cerr << "[ERROR] No matching cameras found\n";
        return 1;
    }

    ImageProcessor processor;
    processor.SetColorProcessing(SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR);

    /* -------- INIT CAMERAS -------- */
    for (auto& cam : cams) {
        std::string sn;
        if (!configure_camera(cam, args, sn)) {
            continue;
        }

        sns.push_back(sn);
        fs::create_directories(args.out_dir + "/" + sn);
        std::cout << "[INFO] Camera " << sn << " ready\n";
    }

    auto period = std::chrono::milliseconds(
        static_cast<int>(1000.0 / args.fps));

    std::cout << "[INFO] Capture started\n";

    /* -------- CAPTURE LOOP -------- */
    while (running) {
        auto loopStart = std::chrono::steady_clock::now();

        for (size_t i = 0; i < cams.size(); i++) {
            try {
                ImagePtr img = cams[i]->GetNextImage(3000);

                if (img->IsIncomplete()) {
                    img->Release();
                    continue;
                }

                ImagePtr bgr = processor.Convert(img, PixelFormat_BGR8);

                cv::Mat frame(
                    bgr->GetHeight(),
                    bgr->GetWidth(),
                    CV_8UC3,
                    bgr->GetData(),
                    bgr->GetStride());

                std::string fname =
                    args.out_dir + "/" + sns[i] +
                    "/cam_" + sns[i] + "_" + timestamp() + ".jpg";

                cv::imwrite(fname, frame);

                bgr->Release();
                img->Release();
            }
            catch (const Spinnaker::Exception& e) {
                std::cerr << "[ERROR] Capture " << sns[i]
                          << ": " << e.what() << "\n";
            }
        }

        std::this_thread::sleep_until(loopStart + period);
    }

    /* -------- CLEANUP -------- */
    for (auto& cam : cams) {
        cam->EndAcquisition();
        cam->DeInit();
    }

    camList.Clear();
    system->ReleaseInstance();

    std::cout << "[INFO] Clean exit\n";
    return 0;
}


// g++ FLIR_CAPTURE.cpp -o flir_multi_capture   -I/opt/spinnaker/include   -L/opt/spinnaker/lib -lSpinnaker   `pkg-config --cflags --libs opencv4`   -std=gnu++17 -pthread
// ./flir_multi_capture   --cams 24033451,23358681   --fps 2   --exposure 5000   --throughput 80000000   --out /home/deepali/OMKAR/CODES/STANDARD/STANDARD_FRAMEWORK/DEBUGS/TEMP_IMG