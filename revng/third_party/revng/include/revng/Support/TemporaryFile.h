#pragma once

//
// This file is distributed under the MIT License. See LICENSE.md for details.
//

#include "llvm/ADT/SmallString.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Process.h"
#include "llvm/Support/Signals.h"

#include "revng/Support/Debug.h"
#include "revng/Support/Error.h"

class TemporaryFile {
private:
  llvm::SmallString<32> Path;

  static void removeFile(llvm::StringRef Path) {
#ifdef _WIN32
    // Fathom: Windows does not delete a read-only file (BinariesContainer
    // makes its files read-only): writable again first
    using namespace llvm::sys::fs;
    setPermissions(Path, perms::owner_read | perms::owner_write);
#endif
    revng::cantFail(llvm::sys::fs::remove(Path));
  }

  TemporaryFile(llvm::StringRef Path, int /* dummy */) : Path(Path.str()) {
    llvm::sys::RemoveFileOnSignal(Path);
  }

public:
  TemporaryFile(const llvm::Twine &Prefix, llvm::StringRef Suffix = "") {
    revng::cantFail(llvm::sys::fs::createTemporaryFile(Prefix, Suffix, Path));
    llvm::sys::RemoveFileOnSignal(Path);
  }

  static llvm::Expected<TemporaryFile> make(const llvm::Twine &Prefix,
                                            llvm::StringRef Suffix = "") {
    using llvm::sys::fs::createTemporaryFile;
    llvm::SmallString<32> TempPath;
    int FD;
    if (auto EC = createTemporaryFile(Prefix, Suffix, FD, TempPath); EC) {
      return llvm::createStringError(EC,
                                     "Could not create temporary file: "
                                       + EC.message());
    }

    // The file is used by its path: close it (Fathom: Windows does not delete
    // an open file, and the descriptor was leaked)
    llvm::sys::Process::SafelyCloseFileDescriptor(FD);

    return TemporaryFile(TempPath, 0);
  }

  TemporaryFile(TemporaryFile &&Other) { *this = std::move(Other); }
  TemporaryFile &operator=(TemporaryFile &&Other) {
    if (this == &Other)
      return *this;

    if (not Path.empty()) {
      removeFile(Path);
      llvm::sys::DontRemoveFileOnSignal(Path);
    }

    Path = Other.Path;
    Other.Path.clear();

    return *this;
  }

  ~TemporaryFile() {
    if (not Path.empty()) {
      removeFile(Path);
      llvm::sys::DontRemoveFileOnSignal(Path);
    }
  }

public:
  TemporaryFile(const TemporaryFile &) = delete;
  TemporaryFile &operator=(const TemporaryFile &) = delete;

public:
  llvm::StringRef path() const {
    revng_assert(Path.size() > 0);
    return Path;
  }
};
