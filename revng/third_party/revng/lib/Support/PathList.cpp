//
// This file is distributed under the MIT License. See LICENSE.md for details.
//

#ifdef _WIN32
// Fathom: Windows (no dl_iterate_phdr)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
extern "C" {
#include "dlfcn.h"
#include "link.h"
}
#endif

#include <map>
#include <set>

#include "llvm/ADT/StringRef.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/ConvertUTF.h"
#include "llvm/Support/GlobPattern.h"
#include "llvm/Support/ManagedStatic.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Process.h"

#include "revng/Support/Assert.h"
#include "revng/Support/Debug.h"
#include "revng/Support/PathList.h"

static Logger Log("find-resources");

struct Resources {
  bool FirstCall = true;
  std::string CurrentRoot;
  std::map<std::string, std::string> LibrariesFullPath;
};

llvm::ManagedStatic<Resources> Resources;

#ifndef _WIN32
extern "C" {
static int
dlIteratePhdrCallback(struct dl_phdr_info *Info, size_t Size, void *Data) {
  using llvm::StringRef;

  if (Info->dlpi_name == nullptr)
    return 0;

  StringRef FullPath(Info->dlpi_name);
  if (FullPath.size() == 0)
    return 0;

  StringRef Name = llvm::sys::path::filename(FullPath);
  Resources->LibrariesFullPath[Name.rsplit(".so").first.str()] = FullPath.str();

  return 0;
}
}
#endif

#ifdef _WIN32
// The module that contains rev.ng (the Python module _pipebox.pyd or a
// program: rev.ng is linked statically into each of them on Windows)
static std::string currentModulePath() {
  HMODULE Module = nullptr;
  GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS
                       | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                     reinterpret_cast<LPCWSTR>(&currentModulePath),
                     &Module);
  std::wstring Buffer(32768, L'\0');
  DWORD Length = GetModuleFileNameW(Module, Buffer.data(), Buffer.size());
  Buffer.resize(Length);
  std::string Result;
  llvm::convertWideToUTF8(Buffer, Result);
  return Result;
}
#endif

static void initialize() {
  if (not Resources->FirstCall)
    return;

  Resources->FirstCall = false;

#ifdef _WIN32
  {
    // The root: the first folder above the module that holds share/revng
    using namespace llvm::sys::path;
    std::string ModulePath = currentModulePath();
    Resources->LibrariesFullPath[stem(ModulePath).str()] = ModulePath;
    llvm::SmallString<256> Root(parent_path(ModulePath));
    while (not Root.empty()) {
      llvm::SmallString<256> Share(Root);
      append(Share, "share", "revng");
      if (llvm::sys::fs::is_directory(Share))
        break;
      std::string Parent = parent_path(Root).str(); // a copy: Root changes
      if (Parent == Root.str())
        break;
      Root = Parent;
    }
    Resources->CurrentRoot = Root.str().str();
  }
#else
  dl_iterate_phdr(dlIteratePhdrCallback, nullptr);

  using namespace llvm::sys::path;
  constexpr const char *MainLibrary = "librevngSupport";
  using llvm::StringRef;
  StringRef MainLibraryFullPath = Resources->LibrariesFullPath.at(MainLibrary);
  Resources->CurrentRoot = parent_path(parent_path(MainLibraryFullPath));
#endif
}

llvm::StringRef getCurrentRoot() {
  initialize();
  return Resources->CurrentRoot;
}

static std::string getXDGDirectory(llvm::StringRef EnvironmentVariable,
                                   llvm::StringRef DefaultInHome,
                                   llvm::StringRef Subdirectory) {
  std::string BaseDir;
  if (auto XDGDir = llvm::sys::Process::GetEnv(EnvironmentVariable)) {
    BaseDir = *XDGDir;
  } else {
    llvm::SmallString<64> PathHome;
    llvm::sys::path::home_directory(PathHome);
    BaseDir = joinPath(PathHome.str(), DefaultInHome);
  }

  return joinPath(BaseDir, Subdirectory);
}

std::string getCacheDirectory() {
  return getXDGDirectory("XDG_CACHE_HOME", ".cache", "revng");
}

std::string getConfigDirectory() {
  return getXDGDirectory("XDG_CONFIG_HOME", ".config", "revng");
}

const std::map<std::string, std::string> &getLibrariesFullPath() {
  initialize();
  return Resources->LibrariesFullPath;
}

static std::optional<std::string>
findFileInPaths(llvm::StringRef FileName,
                const std::vector<std::string> &SearchPaths) {

  std::optional<std::string> FoundFileName;

  for (const auto &Path : SearchPaths) {
    revng_log(Log, "Looking in path: " << Path);
    llvm::SmallString<64> FullFileName;
    llvm::sys::path::append(FullFileName, Path, FileName);

    LoggerIndent Indent(Log);

    if (not llvm::sys::fs::exists(FullFileName)) {
      revng_log(Log, "File not found: " << FullFileName.str().str());
      continue;
    }

    std::error_code
      Err = llvm::sys::fs::access(FullFileName,
                                  llvm::sys::fs::AccessMode::Exist);
    if (Err) {
      revng_log(Log, "Cannot access file: " << FullFileName.str().str());
    } else {
      revng_log(Log, "Found file: " << FullFileName.str().str());
      FoundFileName = FullFileName.str().str();
      break;
    }
  }

  if (not FoundFileName.has_value()) {
    revng_log(Log, "Failed to find `" << FileName.str() << "`.");
  }

  return FoundFileName;
}

std::optional<std::string> PathList::findFile(llvm::StringRef FileName) const {
  return findFileInPaths(FileName, SearchPaths);
}

std::vector<std::string> PathList::list(llvm::StringRef Path,
                                        llvm::StringRef Suffix) const {
  using namespace llvm;
  using namespace sys;
  using namespace fs;
  using std::string;

  std::set<string> Visited;
  std::vector<string> Result;
  for (const string &SearchPath : SearchPaths) {
    SmallString<16> FullPath(SearchPath);
    path::append(FullPath, Path);

    if (is_directory(FullPath)) {
      std::error_code EC;
      for (directory_iterator File(FullPath, EC), FileEnd;
           File != FileEnd && !EC;
           File.increment(EC)) {
        if (llvm::StringRef(File->path()).endswith(Suffix)) {
          bool New = Visited.insert(path::filename(File->path()).str()).second;
          if (New)
            Result.push_back(File->path());
        }
      }

      revng_assert(!EC);
    }
  }

  return Result;
}
